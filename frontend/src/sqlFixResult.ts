import type { RewriteChange } from './performanceResult';
import type { Traced } from './progress';

/**
 * The structured result of an "explain this SQL error" call - mirrors the backend's `SqlFixResult`
 * (see `backend/src/waxwing/nltosqlandback/sql_fix_result.py`) field-for-field, since it's decoded
 * straight off that endpoint's stream.
 *
 * The status says what the panel can offer to do about it:
 * - `Fixed`: there's a corrected query that parses, so it can be put in the editor.
 * - `Explained`: the error is explained but nothing is safe to apply.
 * - `Failure`: the errors couldn't be made sense of against this query.
 */
export type SqlFixStatus = 'Fixed' | 'Explained' | 'Failure';

export interface SqlFixResult extends Traced {
  status: SqlFixStatus;
  /** What the engine's error message means, in plain English. */
  problemSummary: string;
  /** Why this particular query triggered it. */
  cause: string;
  /** What the correction changes, and why that addresses the cause. */
  fixExplanation: string;
  fixedSql: string;
  confidence: string;
  /** What the correction doesn't settle, for the user to check themselves. */
  remainingRisks: string[];
  failureMessage: string;
  /** What the correction changed, each with the SQL either side of it (see `RewriteChange`). */
  changes?: RewriteChange[];
}

/** One error against the editor's query - a gutter annotation from Superset's SQL validator, a
 * syntax error from parsing it (on engines with no validator), or the message the engine came back
 * with when a run was rejected. */
export interface SqlError {
  message: string;
  /** 1-based, as Superset's validator reports them. Absent for an error with no position. */
  line?: number;
  column?: number;
  source: 'sql-validator' | 'parser' | 'query';
}
