---
name: acceptance-tests
description: Design and review Playwright acceptance tests (in `e2e/`) against the four-layer DSL/driver rules. Use before writing a new test or page/component object (design mode), and before calling test work done or when asked to review tests (review mode).
---

# Acceptance tests

Read [e2e/docs/writing-acceptance-tests.md](../../../e2e/docs/writing-acceptance-tests.md) and [e2e/docs/glossary.md](../../../e2e/docs/glossary.md) first. This skill is the procedure; those are the rules and examples.

Work out which mode applies. If invoked with no clear task, ask.

## Design mode — before writing code

1. **State the behaviour in one or two sentences**, in glossary terms, as an analyst would describe it. If you need a term that isn't in the glossary, stop and propose it to the human.
2. **Draft the test case first**, as BDR steps calling DSL methods that may not exist yet. Read it back: would an analyst recognise it? Does any line describe mechanics rather than intent?
3. **List the DSL methods it needs.** For each: does it already exist? Is it on the object the analyst would think of? Does it take and return domain values only? Would its name survive a redesign?
4. **For each new driver member, decide how it will be located** (role + name, label, text…) and inspect the real page to confirm it can be. If it can't, decide who owns the markup:
   - plugin → stop, fill in [e2e/docs/semantic-markup-request.md](../../../e2e/docs/semantic-markup-request.md), and pause this test;
   - Superset → plan a marked exception;
   - unsure → ask.
5. **Check the constraints:** the test creates its own data via the UI, uses unique names, gets the user from a fixture, and doesn't assert on LLM output content.
6. Present the draft test and the DSL method list to the human before implementing anything non-trivial.

## Review mode — before calling work done

Check every changed test and page/component object. Report each finding with its file and line.

**Test cases**
- [ ] No `page`, locators, `expect`, or selector strings in `e2e/tests/features/`
- [ ] Each BDR step body is one or two DSL calls
- [ ] Test names and step titles use glossary terms and describe behaviour, not mechanics

**DSL (public methods)**
- [ ] Named for the analyst's intent; would survive a redesign
- [ ] Take and return domain values — no `Locator`, `Page`, or elements cross the boundary
- [ ] Checks are `expect…` methods that use Playwright's auto-retrying `expect`
- [ ] Live on the object the analyst would think of

**Driver (private members)**
- [ ] All locators and HTML knowledge are `private`
- [ ] Locators use role + name, label or text; related elements are scoped through semantic containers
- [ ] No CSS classes, structure, XPath, unexplained `nth()`/`first()`, or test ids — unless marked `EXCEPTION(...)` with a reason and removal condition
- [ ] Every exception is for **Superset** markup, or was approved by a human
- [ ] No `TODO` standing in for a fix or an exception

**Behaviour**
- [ ] No `waitForTimeout` or fixed sleeps
- [ ] Test sets up its own data through the UI, with unique names, independent of order
- [ ] User from a fixture; no hard-coded credentials
- [ ] No assertions on LLM output content

**Report** in three groups:
1. **Must fix** — breaks a rule with no marked exception.
2. **Needs a human** — a proposed exception, glossary change, markup request, or a rule that looks like it's costing more than it's worth.
3. **Suggestions** — naming or readability improvements.

Don't fix findings in group 2 yourself. In review mode, report before changing anything unless asked to fix.
