"""Making a query faster, and saying why it's slow - from the SQL and the database's plan alone.

Two flows, after waxwing-sql's ``PlanAdvisor``:

- **Accelerate** (`accelerate`): a faster rewrite of the query that returns the same result. With
  "Run query plan first" (`run_plan`, on by default) the database plans the original - never runs
  it - and the model gets that plan, and what it flags, alongside the SQL; without it the model has
  only the SQL, which is quicker and works for a query the database can't plan. Either way the
  rewrite is then checked before anyone sees it (``sql_rewrite_check``): it must parse, the
  database must be able to plan it, and anything code can see that would change the result is
  reported. A rewrite that fails those checks gets one more try, told why.
- **Explain the plan** (`explain_plan`): the database's plan for the query, read back in plain
  English - which steps are expensive, and what would make them cheaper.

Neither needs a semantic schema: for a deployment without one the plan is the only description of
the tables the model gets, and for performance work it is usually the one that matters - which
tables are scanned whole, how big the database thinks they are, which joins it chose.
"""

from __future__ import annotations

import json
from typing import Optional

from . import change_anchors, nlsql_backend, progress, prose, request_context, sql_checks, superset_db
from .cancellation import CancelToken
from .progress import EventStream
from .query_plan import QueryPlan
from .sql_rewrite_check import VERDICT_INVALID, VERDICT_UNCHANGED, RewriteCheck, check_rewrite

REWRITE_FIELD = "rewrittenSql"
EXPLANATION_FIELD = "explanation"


def _accelerate_steps(run_plan: bool) -> progress.Steps:
    steps = [("read", "Read the SQL", "Read the SQL")]
    if run_plan:
        steps.append(("plan", "Asking the database for its plan", "Got the database's plan"))
    steps += [
        ("draft", "Looking for a faster version", "Wrote a faster version"),
        ("check", "Checking the rewrite", "Checked the rewrite"),
    ]
    return progress.Steps(steps)


def accelerate(sql: str, run_plan: bool = True, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """A faster rewrite of `sql`, checked. Returns the payload described in `api.accelerate`."""
    steps = _accelerate_steps(run_plan)
    yield steps.plan_event
    yield steps.done("read", f"{len(sql.splitlines())} lines from the editor")
    database = sql_checks.current_database()
    dialect = database.dialect if database else None

    plan: Optional[QueryPlan] = None
    plan_error: Optional[str] = None
    if run_plan:
        plan, plan_error = yield from _plan_step(database, sql, steps, required=False)

    prompt = _accelerate_prompt(sql, plan)
    rewritten, explanation, changes = yield from _request_rewrite(prompt, steps, cancel_token)

    check = yield from _check_step(sql, rewritten, dialect, plan, database, steps)
    if check.verdict == VERDICT_INVALID:
        problem = check.findings[0].message if check.findings else "it wasn't valid"
        retry_rewritten, retry_explanation, retry_changes = yield from _request_rewrite(
            nlsql_backend.retry_prompt_for(prompt, rewritten, problem),
            steps,
            cancel_token,
            attempt_note="Second attempt - the first rewrite wasn't valid",
        )
        if retry_rewritten:
            retry_check = yield from _check_step(sql, retry_rewritten, dialect, plan, database, steps)
            if retry_check.verdict != VERDICT_INVALID:
                rewritten, explanation, changes, check = retry_rewritten, retry_explanation, retry_changes, retry_check

    # Each change placed in the SQL by the fragments it quotes (see `change_anchors`) - against the
    # rewrite on offer, or the rejected one, which is what they were written about.
    changes = change_anchors.anchor(changes, sql, rewritten)
    change_anchors.note_on_request(changes)

    return {
        "rewrittenSql": rewritten if check.verdict != VERDICT_INVALID else "",
        "rejectedSql": rewritten if check.verdict == VERDICT_INVALID else "",
        "explanation": explanation,
        "changes": [change.to_dict() for change in changes],
        "basedOn": "plan" if plan is not None else "sql",
        "runPlan": run_plan,
        "plan": plan.to_dict() if plan else None,
        "planError": plan_error,
        "rewrittenPlan": check.rewritten_plan.to_dict() if check.rewritten_plan else None,
        "check": check.to_dict(),
    }


def explain_plan(sql: str, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """The database's plan for `sql`, explained. Unlike accelerate, a plan is the whole point here,
    so failing to get one ends the call."""
    steps = progress.Steps([
        ("read", "Read the SQL", "Read the SQL"),
        ("plan", "Asking the database for its plan", "Got the database's plan"),
        ("draft", "Explaining the plan", "Explained the plan"),
    ])
    yield steps.plan_event
    yield steps.done("read", f"{len(sql.splitlines())} lines from the editor")
    database = sql_checks.current_database()
    plan, plan_error = yield from _plan_step(database, sql, steps, required=True)
    if plan is None:
        raise RuntimeError(plan_error or "The database didn't return a plan.")
    text = yield from nlsql_backend._stream_completion(  # noqa: SLF001 - the shared streaming call
        _explain_prompt(sql, plan), steps, None, cancel_token, plain_field=EXPLANATION_FIELD
    )
    return {EXPLANATION_FIELD: text.strip(), "plan": plan.to_dict()}


def _plan_step(
    database: Optional[superset_db.DatabaseInfo], sql: str, steps: progress.Steps, required: bool
) -> EventStream:
    """Asks for `sql`'s plan as step "plan"; returns (plan, error). Best effort unless `required`:
    a failure is reported on the step and the flow carries on without a plan."""
    if database is None:
        message = "No database is selected for this tab (or you can't access it), so there's no plan."
        yield steps.failed("plan", message)
        return None, message
    yield steps.start("plan", f"Planning on {database.name} - the query isn't run")
    context = request_context.current()
    outcome = superset_db.explain(database, sql, context.catalog if context else None, context.schema if context else None)
    if not outcome.ok:
        yield steps.failed("plan", outcome.error)
        return None, outcome.error
    plan = outcome.plan
    facts = []
    if plan.total_cost is not None:
        facts.append(f"estimated cost {plan.total_cost:,.0f}")
    if plan.root is not None:
        facts.append(f"{sum(1 for _ in plan.root.walk())} steps")
    facts.append(f"{len(plan.warnings)} flagged" if plan.warnings else "nothing flagged")
    yield steps.done("plan", ", ".join(facts))
    return plan, None


def _request_rewrite(prompt: str, steps: progress.Steps, cancel_token: Optional[CancelToken], attempt_note: Optional[str] = None) -> EventStream:
    raw = yield from nlsql_backend.stream_json_fields(prompt, steps, [REWRITE_FIELD, EXPLANATION_FIELD], cancel_token, attempt_note)
    payload = json.loads(nlsql_backend.extract_json(raw))
    rewritten = str(payload.get(REWRITE_FIELD) or "").strip()
    if rewritten:
        rewritten = nlsql_backend.strip_sql_fence(rewritten)
    return rewritten, str(payload.get(EXPLANATION_FIELD) or "").strip(), change_anchors.parse_changes(payload.get("changes"))


def _check_step(
    original: str,
    rewritten: str,
    dialect: Optional[str],
    plan: Optional[QueryPlan],
    database: Optional[superset_db.DatabaseInfo],
    steps: progress.Steps,
) -> EventStream:
    yield steps.start("check", "Nothing is written into the editor until you say so")
    if not rewritten:
        check = RewriteCheck(VERDICT_UNCHANGED)
        yield steps.done("check", "No rewrite was suggested")
        return check
    context = request_context.current()

    def plan_on_database(sql: str) -> superset_db.PlanOutcome:
        return superset_db.explain(database, sql, context.catalog if context else None, context.schema if context else None)

    check = check_rewrite(original, rewritten, dialect, plan, plan_on_database if database else None)
    summary = {
        VERDICT_UNCHANGED: "The suggestion is the query as it was",
        VERDICT_INVALID: check.findings[0].message if check.findings else "Not valid",
        "MayDiffer": "It may return different results - see the findings",
        "Unverified": "No difference found in what it returns (not proven equivalent)",
    }.get(check.verdict, check.verdict)
    if check.verdict == VERDICT_INVALID:
        yield steps.failed("check", summary)
    else:
        if check.original_cost is not None and check.rewritten_cost is not None:
            summary += f"; estimated cost {check.original_cost:,.0f} → {check.rewritten_cost:,.0f}"
        yield steps.done("check", summary)
    return check


ACCELERATE_RESPONSE_FORMAT = f"""\
Respond with ONLY a single JSON object (no markdown fences, no other text) with this shape, in this
order:
{{"rewrittenSql": "...", "explanation": "...", {change_anchors.CHANGES_SHAPE}}}

- rewrittenSql: the whole rewritten query, ready to run in place of the original.
- explanation: why it should be faster, in two or three sentences, referring to the plan where
  there is one. Be honest about how much faster: say if the gain is likely small.
{change_anchors.changes_instructions("rewrittenSql", "the original SQL", "why that makes it faster, in a sentence.")}
If the query is already efficient, or nothing can be done without changing the database itself
(an index, statistics), return the query unchanged in rewrittenSql and say so - and what would help
- in explanation. Never change what the query returns: the same rows, the same columns in the same
order, the same ordering and limit.
"""


def _accelerate_prompt(sql: str, plan: Optional[QueryPlan]) -> str:
    parts = [
        "You are a SQL performance expert. Rewrite the following SQL query to run faster while producing\n"
        "exactly the same result.\n",
        nlsql_backend.dialect_note(),
        f"\nOriginal SQL:\n{sql}\n",
    ]
    if plan is not None:
        parts.append(
            "\nQuery plan from the database (the planner's estimates, not a run: each step with its estimated "
            "rows and cost; an indented step feeds the step above it):\n"
            f"{plan.outline}\n"
        )
        if plan.warnings:
            parts.append("\nThe plan was flagged for:\n" + "\n".join(f"- {w.severity}: {w.message}" for w in plan.warnings) + "\n")
    else:
        parts.append("\nThere is no query plan or table information: work from the SQL alone.\n")
    parts.append("\n" + ACCELERATE_RESPONSE_FORMAT)
    parts.append(prose.instructions(
        "explanation and each change's summary and why",
        "explanation in one sentence, each summary a few words, each why a short phrase",
    ))
    return "".join(parts)


def _explain_prompt(sql: str, plan: QueryPlan) -> str:
    warnings = ""
    if plan.warnings:
        warnings = "\nThe plan was flagged for:\n" + "\n".join(f"- {w.severity}: {w.message}" for w in plan.warnings) + "\n"
    return (
        "You are a database performance expert. "
        f"{nlsql_backend.dialect_note()}\n"
        "Explain this query plan to someone who knows SQL but not this engine's internals: what the engine\n"
        "will do, in order, which steps are expensive and why, and what would make it cheaper. Keep it short\n"
        "and concrete; refer to tables and columns by name. Respond in plain text, not JSON.\n\n"
        f"SQL:\n{sql}\n\n"
        "Plan (the planner's estimates: each step with its estimated rows and cost; an indented step feeds the "
        f"step above it):\n{plan.outline}\n"
        f"{warnings}"
        f"{prose.instructions('your explanation', 'two or three sentences in all: the expensive steps and what would make them cheaper')}"
    )
