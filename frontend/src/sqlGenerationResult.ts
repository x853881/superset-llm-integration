import type { RewriteChange } from './performanceResult';
import type { Traced } from './progress';

/**
 * The structured result of an LLM SQL-generation call ("Generate SQL", "Update SQL", "Refine
 * SQL") - mirrors the backend's `SqlGenerationResult` (see
 * `backend/src/waxwing/nltosqlandback/sql_generation_result.py`) field-for-field, since it's
 * decoded straight off that endpoint's JSON response.
 *
 * Only the status-appropriate fields are populated by the model - see
 * `nlsql_backend.SQL_RESPONSE_FORMAT_INSTRUCTIONS` for exactly which:
 * - `Success`: sqlText, sqlDetailedDescription, tablesUsed, suggestionsForRefinement, assumptionsMade.
 * - `ClarificationsRequired`: specificClarificationsNeeded and/or generalClarificationsNeeded.
 * - `Failure`: failureMessage, whatWasTriedOnFailure, suggestionsForFixingOnFailure.
 */
export type SqlGenerationStatus = 'Success' | 'Failure' | 'ClarificationsRequired';

export interface SpecificClarification {
  question: string;
  options: string[];
}

export interface SqlGenerationResult extends Traced {
  /** What a refinement or an update changed, each with the SQL either side of it (see
   * `RewriteChange`) - none for SQL written from scratch, or from a backend from before changes. */
  changes?: RewriteChange[];
  status: SqlGenerationStatus;
  sqlText: string;
  sqlDetailedDescription: string;
  tablesUsed: string[];
  suggestionsForRefinement: string[];
  assumptionsMade: string[];
  specificClarificationsNeeded: SpecificClarification[];
  generalClarificationsNeeded: string[];
  failureMessage: string;
  whatWasTriedOnFailure: string[];
  suggestionsForFixingOnFailure: string[];
}
