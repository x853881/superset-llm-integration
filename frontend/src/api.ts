import { authentication } from '@apache-superset/core';
import { applyEvent, CallProgress, EMPTY_PROGRESS, StreamEvent, TraceRef, Traced } from './progress';
import type { SqlGenerationResult } from './sqlGenerationResult';
import type { SqlError, SqlFixResult } from './sqlFixResult';
import type { AccelerateResult, BreakdownResult, PlanExplanationResult, PlanSummary } from './performanceResult';
import type { SchemaKnowledgeStatus, SchemaKnowledgeSummary } from './schemaKnowledge';
import { getVerbosity } from './verbosity';

const EXTENSION_BASE = '/extensions/waxwing/nltosqlandback';

/**
 * Which SQL Lab tab a call is for, and what that tab points at. Sent with every call: the backend
 * writes and checks SQL in that database's dialect, validates and plans it against that database
 * (see the backend's `sql_checks.py`), and puts all of it - plus the user, which it reads from the
 * session itself - on every log line and trace the call produces. Captured from the tab when the
 * call starts, like everything else about a call (see `tabState.ts`).
 *
 * No schema: queries here qualify every table with its schema, so nothing the extension does
 * depends on which schema the tab happens to have selected.
 */
export interface RequestContext {
  databaseId: number | null;
  catalog: string | null;
  tabId: string;
}

/** Thrown when a call ended because it was cancelled rather than because it failed - the backend
 * says so with a `cancelled` event, since by then the response's status line is long gone. Treated
 * exactly like the client's own `AbortError`: nothing to report, the user asked for it. */
export class CallCancelledError extends Error {
  constructor() {
    super('The request was cancelled.');
    this.name = 'CallCancelledError';
  }
}

/** True for both halves of a "Stop" - the client aborting its own fetch, and the backend reporting
 * that it cancelled the call. Neither is an error to show the user. */
export function isCancellation(error: unknown): boolean {
  return error instanceof CallCancelledError || (error instanceof DOMException && error.name === 'AbortError');
}

async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const csrfToken = await authentication.getCSRFToken();

  const res = await fetch(`${EXTENSION_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken ?? '',
    },
    body: JSON.stringify(body),
    signal,
  });

  const responseBody = await res.json().catch(() => ({}) as any);
  if (!res.ok) {
    throw new Error(responseBody.message || `Request failed (${res.status})`);
  }
  return responseBody.result as T;
}

/**
 * Runs one of the streaming endpoints: POSTs `body`, reads the server-sent events it answers with,
 * reports each one through `onProgress` as it arrives, and resolves with the `result` event's
 * payload once the call lands.
 *
 * Read with `fetch` and a stream reader rather than `EventSource`, which can only issue GETs and
 * so can't carry a request body (nor a CSRF header).
 *
 * A response that arrives all at once instead of progressively - a proxy that buffers it, or a
 * `fetch` implementation without a readable body - still parses correctly here; the progress
 * simply all lands at the end, which is what the panel did before it streamed at all.
 */
async function stream<T>(
  path: string,
  body: unknown,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<T> {
  const csrfToken = await authentication.getCSRFToken();
  const res = await fetch(`${EXTENSION_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken ?? '',
      Accept: 'text/event-stream',
    },
    // Every streaming call is one the model writes prose for, so every one says how long it should
    // be (see `verbosity.ts`).
    body: JSON.stringify({ ...(body as object), verbosity: getVerbosity() }),
    signal,
  });
  if (!res.ok) {
    // A request rejected before the stream started (a validation failure, an expired session) is
    // still an ordinary JSON error response.
    const responseBody = await res.json().catch(() => ({}) as any);
    throw new Error(responseBody.message || `Request failed (${res.status})`);
  }

  let progress = EMPTY_PROGRESS;
  let result: T | undefined;
  let resultSeen = false;

  const handleEvent = (event: StreamEvent) => {
    if (event.type === 'result') {
      result = event.result as T;
      // The trace goes with the result it belongs to, so whatever keeps the result can file
      // feedback on it.
      if (event.trace && result && typeof result === 'object') (result as Traced).trace = event.trace;
      resultSeen = true;
      return;
    }
    if (event.type === 'error') throw new Error(event.message);
    if (event.type === 'cancelled') throw new CallCancelledError();
    progress = applyEvent(progress, event);
    onProgress(progress);
  };

  await readEventStream(res, handleEvent);

  if (!resultSeen) {
    throw new Error('The connection closed before a result arrived.');
  }
  return result as T;
}

/** Feeds each complete server-sent event in `res`'s body to `handleEvent` as it arrives. */
async function readEventStream(res: Response, handleEvent: (event: StreamEvent) => void): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = '';

  const consume = (chunk: string, flushing: boolean) => {
    // Carriage returns are stripped rather than handled as a second line terminator: every
    // payload here is single-line JSON, so any raw \r is part of the framing, not the data.
    buffer += chunk.replace(/\r/g, '');
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      handleFrame(buffer.slice(0, boundary), handleEvent);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf('\n\n');
    }
    // A last event that arrived without its terminating blank line is still worth reading -
    // notably the `result` the whole call is waiting on.
    if (flushing && buffer.trim() !== '') {
      handleFrame(buffer, handleEvent);
      buffer = '';
    }
  };

  const reader = res.body?.getReader();
  if (!reader) {
    consume(await res.text(), true);
    return;
  }
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    consume(decoder.decode(value, { stream: true }), false);
  }
  consume(decoder.decode(), true);
}

/** Parses one event - its `data:` lines joined - and hands it over. A frame that isn't valid JSON
 * (a keep-alive comment, a truncated line) is skipped rather than failing the call. */
function handleFrame(frame: string, handleEvent: (event: StreamEvent) => void): void {
  const data = frame
    .split('\n')
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice('data:'.length).trim())
    .join('');
  if (!data) return;
  let event: StreamEvent;
  try {
    event = JSON.parse(data) as StreamEvent;
  } catch {
    return;
  }
  handleEvent(event);
}

/**
 * Cancels the call previously started with this same `requestId` - forcibly closing its
 * connection to the LLM provider server-side, not just abandoning the client's wait on it. Fired
 * alongside aborting the client-side fetch itself (see `NLSqlHelperPanel`'s `handleStop`); best
 * effort, so a call that's already finished by the time this lands is not treated as an error.
 */
export async function cancelRequest(requestId: string): Promise<void> {
  await post<{ cancelled: boolean }>('/cancel', { requestId });
}

/** Cold start: generate SQL for a natural-language request, with no prior SQL to ground against. */
export async function generateSql(
  naturalLanguage: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<SqlGenerationResult> {
  return stream<SqlGenerationResult>('/generate-sql', { ...context, naturalLanguage, requestId }, onProgress, signal);
}

/**
 * The request changed from `previousNl` to `newNl` after `previousSql` had already been generated
 * for `previousNl`. Returns updated SQL grounded in `previousSql` - an edit, not a fresh
 * translation of `newNl` alone.
 */
export async function updateSqlFromNl(
  previousNl: string,
  previousSql: string,
  newNl: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<SqlGenerationResult> {
  return stream<SqlGenerationResult>(
    '/update-sql-from-nl',
    { ...context, previousNl, previousSql, newNl, requestId },
    onProgress,
    signal,
  );
}

/** Applies the free-text instruction `command` to the current `sql`, independent of whatever
 * natural-language request (if any) produced it. */
export async function refineSql(
  command: string,
  sql: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<SqlGenerationResult> {
  return stream<SqlGenerationResult>('/refine-sql', { ...context, command, sql, requestId }, onProgress, signal);
}

/** Cold start: describe a SQL query in natural language, with no prior description to ground against. */
export async function generateNl(
  sql: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<Description> {
  const result = await stream<{ naturalLanguage: string } & Traced>(
    '/generate-nl',
    { ...context, sql, requestId },
    onProgress,
    signal,
  );
  return { text: result.naturalLanguage ?? '', trace: result.trace };
}

/**
 * The SQL changed from `previousSql` to `newSql` while it was described by `previousNl`. Returns
 * an updated description conditioned on `previousNl` and the diff between the two SQL versions,
 * rather than re-describing `newSql` independently.
 */
export async function updateNlFromSqlDiff(
  previousNl: string,
  previousSql: string,
  newSql: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<Description> {
  const result = await stream<{ naturalLanguage: string } & Traced>(
    '/update-nl-from-sql-diff',
    { ...context, previousNl, previousSql, newSql, requestId },
    onProgress,
    signal,
  );
  return { text: result.naturalLanguage ?? '', trace: result.trace };
}

/** What Describe writes: the description, and the request it came from. */
export interface Description extends Traced {
  text: string;
}

/** What someone thought of one result (see `feedback.ts`) - filed under that result's trace. */
export interface FeedbackBody {
  traceId: string;
  spanId: string;
  feedbackId: string;
  rating: 'up' | 'down';
  target: string;
  reason?: string;
  label?: string;
  comment?: string;
  variant: Record<string, string>;
}

export async function sendFeedback(body: FeedbackBody): Promise<void> {
  await post<{ recorded: boolean }>('/feedback', body);
}

export type { TraceRef };

/**
 * Explains the errors the editor is showing against `sql` and proposes a corrected version of it.
 * `dialect` is the database's engine name where the caller knows it, since half of what makes an
 * error message confusing is that it's the engine's wording rather than standard SQL's.
 */
export async function explainSqlError(
  sql: string,
  errors: SqlError[],
  dialect: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<SqlFixResult> {
  return stream<SqlFixResult>(
    '/explain-sql-error',
    { ...context, sql, errors, dialect, requestId },
    onProgress,
    signal,
  );
}

/**
 * A faster rewrite of `sql` that returns the same result, checked by the backend before it comes
 * back (see `performance.py`). With `runPlan` the database plans the original first - it is never
 * run - and the model gets that plan as well as the SQL.
 */
export async function accelerateSql(
  sql: string,
  runPlan: boolean,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<AccelerateResult> {
  return stream<AccelerateResult>('/accelerate', { ...context, sql, runPlan, requestId }, onProgress, signal);
}

/** The database's plan for `sql` (not run), explained in plain English. */
export async function explainPlan(
  sql: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<PlanExplanationResult> {
  return stream<PlanExplanationResult>('/explain-plan', { ...context, sql, requestId }, onProgress, signal);
}

/** `sql` described section by section, each section with the lines it covers. With `formatSql`
 * the backend lays the query out one clause per line first, and the result's `sql` is that layout
 * (which its line numbers refer to). */
export async function describeBreakdown(
  sql: string,
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
  formatSql = false,
): Promise<BreakdownResult> {
  return stream<BreakdownResult>('/describe-breakdown', { ...context, sql, requestId, formatSql }, onProgress, signal);
}

/** The database's plan for `sql`, without running it and without involving a model. With
 * `includeTree`, the plan's whole node tree too (the Query Estimator draws it). */
export async function fetchQueryPlan(
  sql: string,
  context: RequestContext,
  includeTree = false,
): Promise<{ plan?: PlanSummary; error?: string; checked?: boolean }> {
  return post('/query-plan', { ...context, sql, includeTree });
}

/** `sql` laid out one clause per line in the database's dialect; unchanged when it doesn't parse. */
export async function formatSql(sql: string, context: RequestContext): Promise<{ sql: string; formatted: boolean }> {
  return post('/format-sql', { ...context, sql });
}

/** Reads the structure of every schema of the tab's database; kept for every later prompt about it. */
export async function interrogateDb(
  context: RequestContext,
  requestId: string,
  onProgress: (progress: CallProgress) => void,
  signal?: AbortSignal,
): Promise<SchemaKnowledgeSummary> {
  return stream<SchemaKnowledgeSummary>('/interrogate-db', { ...context, requestId }, onProgress, signal);
}

/** Whether (and when, and by whom) the tab's database has been interrogated; with `full`, the tables. */
export async function fetchDbKnowledge(context: RequestContext, full = false): Promise<SchemaKnowledgeStatus> {
  const params = new URLSearchParams();
  if (context.databaseId !== null) params.set('databaseId', String(context.databaseId));
  if (context.catalog) params.set('catalog', context.catalog);
  if (full) params.set('full', 'true');
  const res = await fetch(`${EXTENSION_BASE}/db-knowledge?${params.toString()}`, { headers: { Accept: 'application/json' } });
  const responseBody = await res.json().catch(() => ({}) as any);
  if (!res.ok) throw new Error(responseBody.message || `Request failed (${res.status})`);
  return responseBody.result as SchemaKnowledgeStatus;
}

/** Throws away what was found in the tab's database. */
export async function forgetDbKnowledge(context: RequestContext): Promise<boolean> {
  const result = await post<{ forgotten: boolean }>('/db-knowledge/forget', context);
  return result.forgotten;
}

/** One finding from Superset's SQL validator, as the extension's `/validate-sql` reports it. */
interface ValidationMessage {
  message?: string;
  line?: number | null;
  column?: number | null;
}

/** Raised by `validateSql` when the database can't be checked at all - no SQL validator configured,
 * and a dialect the backend can't parse in. Not a failure to report, just nothing to check with, and
 * a reason to stop asking about that database. */
export class NoValidatorError extends Error {
  constructor() {
    super('No SQL validator is configured for this database.');
    this.name = 'NoValidatorError';
  }
}

/**
 * Runs `sql` past Superset's own SQL validator - the same validator SQL Lab's editor uses for the
 * error markers in its gutter, so what comes back here is what the user is looking at (see
 * `superset-frontend/src/SqlLab/components/EditorWrapper/useAnnotations.ts`).
 *
 * Asked through the extension's backend rather than Superset's `validate_sql` endpoint directly so
 * that it is traced like every other call the panel makes (a `validate_sql` request with a
 * `db · validate` span under it). Asking at all, rather than reading the markers out of the editor,
 * because the extension API's editor handle can set annotations but not read them back.
 *
 * Superset has validators for only a few engines (Postgres and Presto). For the rest the backend
 * parses the SQL itself in the database's dialect and says so (`checkedBy: "parser"`): syntax
 * errors only, with no gutter markers behind them. Only a database whose dialect it can't tell
 * comes back as a 422, which gets its own error type so the caller can stop asking, rather than
 * reading as a transient failure worth retrying on every keystroke.
 */
export async function validateSql(sql: string, context: RequestContext, signal?: AbortSignal): Promise<SqlError[]> {
  const csrfToken = await authentication.getCSRFToken();
  const res = await fetch(`${EXTENSION_BASE}/validate-sql`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken ?? '',
    },
    body: JSON.stringify({ ...context, sql }),
    signal,
  });
  if (res.status === 422) throw new NoValidatorError();
  const responseBody = await res.json().catch(() => ({}) as any);
  if (!res.ok) {
    throw new Error(responseBody.message || `Could not validate the SQL (${res.status})`);
  }
  const messages: ValidationMessage[] = responseBody.result ?? [];
  // Where the engine has no validator the backend parses the SQL itself - syntax errors only.
  const source = responseBody.checkedBy === 'parser' ? ('parser' as const) : ('sql-validator' as const);
  return messages
    .filter(message => (message.message ?? '').trim() !== '')
    .map(message => ({
      message: (message.message ?? '').trim(),
      line: message.line ?? undefined,
      column: message.column ?? undefined,
      source,
    }));
}

/** Engine names already looked up, keyed by database id - a database's engine doesn't change. */
const backendByDatabaseId = new Map<number, string>();

/**
 * The database's engine name ("postgresql", "trino", ...), for telling the model which dialect's
 * error message it is being asked to explain. Read from Superset's own database API because the
 * extension API's `Database` carries only an id, a name and its catalogs and schemas.
 *
 * Best effort: an engine name is worth having but nothing here depends on it, so a failure to read
 * it resolves to "" rather than failing the call it was wanted for.
 */
export async function fetchDatabaseBackend(databaseId: number): Promise<string> {
  const cached = backendByDatabaseId.get(databaseId);
  if (cached !== undefined) return cached;
  let backend = '';
  try {
    const res = await fetch(`/api/v1/database/${databaseId}`, { headers: { Accept: 'application/json' } });
    if (res.ok) {
      const responseBody = await res.json();
      backend = responseBody?.result?.backend ?? '';
    }
  } catch {
    backend = '';
  }
  backendByDatabaseId.set(databaseId, backend);
  return backend;
}
