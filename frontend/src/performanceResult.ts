import type { Traced } from './progress';

/**
 * The results of the performance and breakdown calls - mirrors of what the backend's
 * `performance.py` and `sql_breakdown.py` return, field for field.
 */

/** A query plan as the backend summarises it (`query_plan.QueryPlan.to_dict`). */
export interface PlanSummary {
  backend: string;
  /** The plan as an indented outline - the same text the model was shown. */
  outline: string;
  totalCost: number | null;
  costUnit: string;
  warnings: { severity: 'warning' | 'info'; code: string; message: string }[];
  steps: number | null;
  /** The whole node tree - only when asked for (`fetchQueryPlan(..., includeTree)`). */
  tree?: PlanNode;
  /** How long the database took to plan it. */
  durationMs?: number;
  /** The planner's own run-time estimate, where the engine gives one (Oracle). */
  estimatedSeconds?: number | null;
}

/** One step of a plan, in the shape common to every engine (`query_plan.PlanNode.to_dict`). */
export interface PlanNode {
  operation: string;
  objectName: string | null;
  estimatedRows: number | null;
  cost: number | null;
  details: Record<string, string>;
  children: PlanNode[];
}

/** How far a rewrite was checked, weakest evidence of a problem first - see `sql_rewrite_check.py`. */
export type RewriteVerdict = 'Invalid' | 'MayDiffer' | 'Unverified' | 'Unchanged';

export interface RewriteCheck {
  verdict: RewriteVerdict;
  findings: { severity: 'error' | 'warning' | 'info'; message: string }[];
  originalCost: number | null;
  rewrittenCost: number | null;
  costUnit: string | null;
}

/**
 * One change Accelerate made: what, why, and the SQL either side of it - quoted, so it can be found
 * again in whichever SQL it is shown against (see `changeNotes.ts`, and the backend's
 * `change_anchors.py`, which has already checked each quote is really there). Either fragment is ""
 * when there is none: an addition has no `before`, a removal no `after`, and a quote that couldn't
 * be found is dropped rather than left to point somewhere wrong.
 */
export interface RewriteChange {
  summary: string;
  why: string;
  before: string;
  after: string;
}

export interface AccelerateResult extends Traced {
  /** The checked rewrite - empty when it failed its checks (see `rejectedSql`). */
  rewrittenSql: string;
  /** A rewrite that failed its checks even after a second attempt, kept only to say so. */
  rejectedSql: string;
  explanation: string;
  changes: RewriteChange[];
  /** Whether the model had the database's plan to go on, or only the SQL. */
  basedOn: 'plan' | 'sql';
  runPlan: boolean;
  plan: PlanSummary | null;
  planError: string | null;
  rewrittenPlan: PlanSummary | null;
  check: RewriteCheck;
}

export interface PlanExplanationResult extends Traced {
  explanation: string;
  plan: PlanSummary;
}

export type BreakdownKind =
  | 'cte'
  | 'select'
  | 'source'
  | 'join'
  | 'filter'
  | 'aggregate'
  | 'window'
  | 'order'
  | 'limit'
  | 'subquery'
  | 'other';

export interface BreakdownSection {
  title: string;
  /** One sentence. */
  description: string;
  /** A fuller explanation, shown on request; may be empty (older results). */
  detail?: string;
  /** 1-based, inclusive. */
  startLine: number;
  endLine: number;
  kind: BreakdownKind;
  /** The stored tables it reads, from Interrogate DB (not the model) - absent before that's run. */
  tables?: BreakdownTable[];
  /** What's worth knowing about its joins and filters - a large table read on an unindexed column. */
  hints?: string[];
}

export interface BreakdownTable {
  schema: string;
  name: string;
  kind: string;
  /** The planner's estimate, when Interrogate DB could get one. */
  rows: number | null;
}

export interface BreakdownResult extends Traced {
  summary: string;
  sections: BreakdownSection[];
  /** The SQL the sections' line numbers refer to - the editor's, or that laid out by the backend. */
  sql?: string;
  /** Whether `sql` is the backend's layout of the editor's SQL rather than the editor's own text. */
  formatted?: boolean;
  /** Written with Brief on (see `verbosity.ts`): its sections have no detailed explanation. */
  brief?: boolean;
}
