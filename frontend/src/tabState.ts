import { useCallback, useEffect, useRef, useState } from 'react';
import { cancelRequest } from './api';
import { CallProgress, EMPTY_PROGRESS, TraceRef } from './progress';
import type { SqlGenerationResult } from './sqlGenerationResult';
import type { SqlError, SqlFixResult } from './sqlFixResult';
import type { AccelerateResult, BreakdownResult, PlanExplanationResult } from './performanceResult';

export type EditType = 'generate' | 'refine' | 'manual' | 'fix' | 'accelerate';

/**
 * The four jobs this panel does, each of which owns the whole column when it's the one on show
 * (in the tabs layout - see `layouts/`): turning a question into SQL, editing that SQL by
 * instruction, explaining the errors the editor is showing against it, and reading SQL back as a
 * question.
 *
 * Separate workflows rather than one column that runs them all at once, because at 400px wide they
 * were competing for the same region - and because only one of them is ever the thing the user came
 * here to do. What a workflow that *isn't* on show has to say is carried on its own tab instead: a
 * spinner while it's running, a count of outstanding problems, a dot for a result that landed while
 * the user was looking somewhere else.
 */
export type WorkflowSegment = 'ask' | 'refine' | 'fix' | 'describe';

export const WORKFLOW_SEGMENTS: WorkflowSegment[] = ['ask', 'refine', 'fix', 'describe'];

/**
 * The tab the panel opens on. Refine rather than Ask, because the deployments this is first aimed
 * at have no semantic description of their databases: without one, writing a query from a question
 * is the weakest thing the panel does, while describing, refining and speeding up a query that
 * already exists need nothing but the query (and its plan).
 */
export const DEFAULT_SEGMENT: WorkflowSegment = 'refine';

/** Which workflow a step in the edit timeline belongs to, so stepping onto it can show it where it
 * happened. A hand edit belongs to no workflow in particular, so it stays where the user is. */
export function segmentForEditType(type: EditType): WorkflowSegment | null {
  if (type === 'generate') return 'ask';
  if (type === 'refine' || type === 'accelerate') return 'refine';
  if (type === 'fix') return 'fix';
  return null;
}

/** Superset's own "new editor" default is treated as empty everywhere in this panel, the same as "". */
export function isSqlEmpty(sql: string): boolean {
  const trimmed = sql.trim();
  return trimmed === '' || trimmed === 'SELECT ...';
}

/**
 * What an in-flight call is doing, so the progress display can say so rather than showing a bare
 * spinner - a cold `generate` and a `update` grounded in SQL that's already there read very
 * differently to someone watching, as do `refine` and `describe` (the SQL -> words direction).
 */
export type CallKind =
  | 'generate'
  | 'update'
  | 'refine'
  | 'describe'
  | 'fix'
  | 'accelerate'
  | 'explain-plan'
  | 'breakdown'
  | 'interrogate';

/** Which workflow tab a call of `kind` belongs to - where its progress and result are shown. */
export function segmentForCallKind(kind: CallKind): WorkflowSegment {
  if (kind === 'generate' || kind === 'update') return 'ask';
  if (kind === 'refine' || kind === 'accelerate' || kind === 'explain-plan') return 'refine';
  if (kind === 'fix') return 'fix';
  return 'describe';
}

export interface GroundedPair {
  nl: string;
  sql: string;
}

/**
 * One step in the undo/redo timeline - an LLM-backed attempt (`generate` or `refine`, carrying
 * whatever `SqlGenerationResult` it produced, success or not), a hand edit detected before one
 * (`manual`), or a correction applied from the SQL-error workflow (`fix`); the last two have no
 * `SqlGenerationResult` of their own. `oldSql`/`newSql` are equal for an attempt that
 * didn't change the SQL (a `ClarificationsRequired` or `Failure` result) - it's still a real step
 * so its detail isn't lost, it just doesn't move the editor when stepped onto.
 */
export interface EditEntry {
  type: EditType;
  command: string | null;
  oldSql: string;
  newSql: string;
  result: SqlGenerationResult | null;
}

/**
 * What to show for one `EditEntry` - the current one just acted on, or one landed on by stepping
 * through undo/redo. `command` is the refine instruction (or natural-language request) that
 * produced this step, if any; `result` is null for a manual edit, since a hand edit has no
 * generation result to show.
 */
export interface GenerationDisplay {
  type: EditType;
  command: string | null;
  result: SqlGenerationResult | null;
}

/**
 * A `SqlGenerationResult` with status `ClarificationsRequired`, plus enough of the call that
 * produced it to retry with the same grounding once every clarification has been answered.
 */
export interface PendingClarifications {
  kind: 'generate' | 'refine';
  originalInput: string;
  groundedAtStart: GroundedPair | null;
  sqlAtStart: string;
  result: SqlGenerationResult;
}

/**
 * A call in flight - both halves of what a "Stop" button needs to actually kill it: `controller`
 * aborts the client's own fetch, and `requestId` (sent as part of the request body) is what the
 * backend registered its own cancellation hook under, so `api.cancelRequest` can reach it too.
 */
export interface InFlightCall {
  requestId: string;
  controller: AbortController;
  kind: CallKind;
  /** When the call went out (epoch ms), for the elapsed-time readout beside it. */
  startedAt: number;
  /** What the backend has reported about it so far - which stage it has reached, and the answer as
   * far as the model has written it. Lives here, in the acting tab's state, rather than in the
   * component: switching tabs mid-call and coming back should show the call where it actually is
   * (see `useTabState`). */
  progress: CallProgress;
}

/**
 * The SQL-error workflow, which starts from something outside this panel objecting to the query in
 * the editor: Superset's SQL validator marking it up (the same annotations shown in the editor's
 * gutter) or a run the database rejected.
 *
 * Separate from the ask/refine flow's state because it's a separate job with its own beginning -
 * there's no question here, and the SQL already exists - and because it has to survive that flow
 * happening around it. Replaced wholesale when the errors change, so an explanation is never shown
 * against a different set of errors than the one it was asked about.
 */
export interface FixWorkflow {
  errors: SqlError[];
  /** The SQL the errors were reported against, so a result can be recognised as stale. */
  sql: string;
  /** Set for the duration of the "explain this error" call - null before it's asked for. */
  call: InFlightCall | null;
  result: SqlFixResult | null;
  /** True once the user has closed the card for these errors - a new set of errors reopens it. */
  dismissed: boolean;
}

/** A breakdown, and the editor's SQL it was asked for (trailing whitespace trimmed - see
 * `breakdownAction.breakdownSource`). */
export interface BreakdownEntry {
  result: BreakdownResult;
  sql: string;
}

/** How many breakdowns a tab remembers. */
export const MAX_REMEMBERED_BREAKDOWNS = 12;

/** The breakdown to show for `source`, the editor's SQL now: one made from exactly that SQL if this
 * tab has one, otherwise the latest (which is then out of date). */
export function breakdownFor(state: TabState, source: string): BreakdownEntry | null {
  if (state.breakdown === null || state.breakdown.sql === source) return state.breakdown;
  return state.breakdownHistory.find(entry => entry.sql === source) ?? state.breakdown;
}

/** `state` with `entry` as its latest breakdown, remembered for `breakdownFor`. */
export function rememberBreakdown(state: TabState, entry: BreakdownEntry): TabState {
  const history = [entry, ...state.breakdownHistory.filter(previous => previous.sql !== entry.sql)];
  return { ...state, breakdown: entry, breakdownHistory: history.slice(0, MAX_REMEMBERED_BREAKDOWNS) };
}

/** Everything that identifies one set of errors, so a result or a dismissal can be matched to it. */
export function fixSignature(sql: string, errors: SqlError[]): string {
  return JSON.stringify([sql, errors.map(error => [error.line, error.column, error.message, error.source])]);
}

/** Everything this helper panel keeps per SQL Lab tab - see `useTabState` for why. */
export interface TabState {
  nlText: string;
  refineCommand: string;
  grounded: GroundedPair | null;
  generatingSql: boolean;
  generatingNl: boolean;
  // Set for the duration of the in-flight "generate/update/refine SQL" or "generate/update
  // description" call (respectively), so a "Stop" button can abort it. Null once that call
  // settles (success, failure, or abort) - see `generateSqlAction`/`refineSqlAction`/
  // `handleUpdateNl` in `panelModel.tsx`.
  sqlCall: InFlightCall | null;
  nlCall: InFlightCall | null;
  // The performance calls (accelerate, explain the plan) - their own slot, since neither changes
  // the SQL in the editor and both can usefully be asked for while a refinement result is showing.
  perfCall: InFlightCall | null;
  // The SQL-error workflow's own state, independent of the ask/refine flow above - null when the
  // editor's SQL has nothing wrong with it (see `FixWorkflow`).
  fix: FixWorkflow | null;
  // Which workflow is currently showing, and which of the others have something the user hasn't
  // seen - a call that finished while they were on a different tab.
  segment: WorkflowSegment;
  unread: Record<WorkflowSegment, boolean>;
  // The last description produced by the describe workflow, and the SQL it was produced from -
  // kept apart from `grounded`, which a generation sets too, so that tab only ever shows something
  // this workflow actually wrote.
  description: { text: string; sql: string; trace?: TraceRef } | null;
  // The section-by-section description, and the SQL it describes (shown read-only beside it, since
  // the editor's own text may have moved on).
  breakdown: BreakdownEntry | null;
  // The last few breakdowns of this tab's SQL, newest first (the latest included), so going back to
  // SQL that was broken down before - an undo, say - shows its breakdown again without asking the
  // model. See `breakdownFor`.
  breakdownHistory: BreakdownEntry[];
  // The last faster rewrite suggested for this tab, and the SQL it is a rewrite of.
  accelerate: { result: AccelerateResult; sql: string } | null;
  // The last explanation of this tab's query plan, and the SQL it was planned for.
  planExplanation: { result: PlanExplanationResult; sql: string } | null;
  // Which of the Refine tab's results is on show - the most recent one of them to land.
  refineView: 'refine' | 'accelerate' | 'explain-plan';
  // "Run query plan first" beside Accelerate - per tab, on by default (see `performance.py`).
  accelerateRunPlan: boolean;
  error: string;
  editHistory: EditEntry[];
  editPosition: number;
  // null until the panel has actually written SQL into the editor at least once - there's no
  // baseline to diff a manual edit against before that, so a manual edit is only ever recorded
  // once this has a value (undoing back to a phantom "" entry would otherwise wipe out SQL the
  // user typed by hand before ever touching the panel).
  lastAppliedSql: string | null;
  // SQL waiting to be written into this tab's *real* editor - set instead of calling
  // `editor.setValue()` directly, because the target tab's editor may not be there to call: the
  // host keeps a live editor handle only for the tab on screen, so a call that finishes after the
  // user has switched to another tab has nothing it can safely write to. Written by the panel the
  // moment this tab is active and its editor has resolved (see the `pendingSql` effect in
  // `panelModel.tsx`) - straight away if that's now, whenever the user comes back otherwise.
  // Ported from sql-helper, where it fixed SQL landing in the wrong tab (or nowhere).
  pendingSql: string | null;
  generationDisplay: GenerationDisplay | null;
  pendingClarifications: PendingClarifications | null;
  specificAnswers: Record<number, string>;
  generalAnswers: Record<number, string>;
  // True while the user is back in the question box with a result already on screen ("Edit
  // question"/"Rephrase"), which is the only case the composer can't be inferred from the rest of
  // the state - before the first result there's nothing else to show, and during a call the
  // progress display takes over regardless.
  editingQuestion: boolean;
}

function createInitialTabState(): TabState {
  return {
    nlText: '',
    refineCommand: '',
    grounded: null,
    generatingSql: false,
    generatingNl: false,
    sqlCall: null,
    nlCall: null,
    perfCall: null,
    fix: null,
    segment: DEFAULT_SEGMENT,
    unread: { ask: false, refine: false, fix: false, describe: false },
    description: null,
    breakdown: null,
    breakdownHistory: [],
    accelerate: null,
    planExplanation: null,
    refineView: 'refine',
    accelerateRunPlan: true,
    error: '',
    editHistory: [],
    editPosition: 0,
    lastAppliedSql: null,
    pendingSql: null,
    generationDisplay: null,
    pendingClarifications: null,
    specificAnswers: {},
    generalAnswers: {},
    editingQuestion: false,
  };
}

/**
 * Backs every SQL Lab tab's own copy of this extension's panel state (the question and refine
 * text, undo/redo history, the last generation result, ...) with a module-level store
 * keyed by `sqlLab.Tab.id` - not component-local `useState`.
 *
 * This panel is registered at the `sqllab.rightSidebar` view location (see `index.tsx`) rather
 * than replacing SQL Lab's own editor, which means Superset mounts exactly *one* instance of it
 * for the whole SQL Lab session - not one per tab. Switching tabs never remounts it; it just fires
 * `sqlLab.onDidChangeActiveTab` and leaves this component to notice and update itself (see
 * `useActiveSqlLabTab` in `panelModel.tsx`). So component-local state would be shared by
 * every SQL Lab tab, showing tab A's question text and undo/redo history while tab B's editor is
 * active. Keying explicitly by `sqlLab.Tab.id` here is what makes each tab show its own state
 * instead. Concretely, this also means a request started from tab A must still land in tab A's
 * own state even if the user has switched to tab B (or beyond) before it resolves - never into
 * "whichever tab happens to be showing right now". `updateTabState` (addressed by an explicitly
 * captured id) exists for exactly that: any state change made from inside an `async` continuation
 * should go through it with the id captured before the first `await`, rather than through the
 * id-implicit updater `useTabState` returns, which is only safe for synchronous handlers (nothing
 * can switch tabs mid-synchronous-call).
 */
const tabStates = new Map<string, TabState>();
const listeners = new Map<string, Set<(state: TabState) => void>>();

function loadTabState(id: string): TabState {
  const existing = tabStates.get(id);
  if (existing) return existing;
  const created = createInitialTabState();
  tabStates.set(id, created);
  return created;
}

/**
 * Applies `updater` to the state stored for `id`, independent of whether any component currently
 * displays that tab, and notifies any hook currently subscribed to `id` (there's normally at most
 * one, but this handles multiple) so it re-renders with the result. Use this - with `id` captured
 * up front - for state changes made after an `await` inside an async action, so a background
 * completion always writes back to the tab it was started for.
 */
export function updateTabState(id: string, updater: (prev: TabState) => TabState): void {
  const next = updater(loadTabState(id));
  tabStates.set(id, next);
  listeners.get(id)?.forEach(listener => listener(next));
}

/** Appends `entry` to `id`'s undo/redo timeline, truncating any redo tail past the current position. */
export function pushEdit(id: string, entry: EditEntry): void {
  updateTabState(id, prev => {
    const editHistoryNext = prev.editHistory.slice(0, prev.editPosition);
    editHistoryNext.push(entry);
    return { ...prev, editHistory: editHistoryNext, editPosition: editHistoryNext.length };
  });
}

/** Records a hand edit of `id`'s SQL (found to differ from its last LLM-applied SQL) as its own timeline step. */
export function recordManualEditIfAny(id: string, currentSql: string): void {
  updateTabState(id, prev => {
    if (prev.lastAppliedSql === null || currentSql === prev.lastAppliedSql) return prev;
    const editHistoryNext = prev.editHistory.slice(0, prev.editPosition);
    editHistoryNext.push({ type: 'manual', command: null, oldSql: prev.lastAppliedSql, newSql: currentSql, result: null });
    return { ...prev, editHistory: editHistoryNext, editPosition: editHistoryNext.length };
  });
}

/** Aborts both halves of every call `state` has in flight (client-side fetch and the backend's own
 * cancellation registry) - the same mechanism as the panel's "Stop" button, but usable from tab
 * lifecycle handling below, where there's no button to click and no live editor to act on. */
export function stopInFlightCalls(state: TabState): void {
  [state.sqlCall, state.nlCall, state.perfCall, state.fix?.call ?? null].forEach(stopCall);
}

/** Aborts one call, both halves: the client's own fetch, and the backend's connection to the LLM
 * provider (which keeps running - and, with a local model, keeps a machine busy - otherwise). */
export function stopCall(call: InFlightCall | null): void {
  if (!call) return;
  call.controller.abort();
  cancelRequest(call.requestId).catch(() => {}); // best effort - the call may already be done
}

/**
 * Resets `id` to a fresh `TabState` - because a brand new SQL Lab tab was created under this id,
 * which should never show request text, grounding, or undo/redo history left over from a stale
 * entry (this id has never been used before in the ordinary case, but defends against it anyway).
 * Any call still in flight under a stale entry is stopped first, since it has nowhere left to
 * apply its result once the state it was writing to is gone.
 */
export function resetTabState(id: string): void {
  const existing = tabStates.get(id);
  if (existing) stopInFlightCalls(existing);
  updateTabState(id, () => createInitialTabState());
}

/**
 * Drops `id`'s stored state entirely - because that SQL Lab tab was closed and there's nothing
 * left to show for it. Stops any call still in flight for it first (there's no tab left for its
 * result to land in), then removes it from the store so it doesn't linger forever as a closed
 * tab's dead entry.
 */
export function disposeTabState(id: string): void {
  const existing = tabStates.get(id);
  if (existing) stopInFlightCalls(existing);
  tabStates.delete(id);
  listeners.delete(id);
}

/** Shows `segment`, and clears its unread mark - whatever it was holding has now been seen. */
export function showSegment(id: string, segment: WorkflowSegment): void {
  updateTabState(id, prev => ({ ...prev, segment, unread: { ...prev.unread, [segment]: false } }));
}

/**
 * Marks `segment` as having something new, unless it's the one already on show. Called when a call
 * finishes: the whole cost of giving each workflow its own tab is that three of them are off screen
 * at any moment, and this is what stops a result landing there unnoticed.
 */
export function markUnread(id: string, segment: WorkflowSegment): void {
  updateTabState(id, prev =>
    prev.segment === segment ? prev : { ...prev, unread: { ...prev.unread, [segment]: true } },
  );
}

/**
 * Records what Superset's SQL validator makes of `sql` - the same annotations the editor draws in
 * its gutter (see `sqlValidation`).
 *
 * Leaves an error reported by a *failed run* alone: the validator has nothing to say about a query
 * the database rejected at execution time (permissions, a missing table it can't see, a timeout),
 * so its silence is not evidence that the problem has gone away. That one is cleared by the SQL
 * changing instead.
 */
export function recordValidatorErrors(id: string, sql: string, errors: SqlError[]): void {
  updateTabState(id, prev => {
    const fix = prev.fix;
    // Never pull the errors out from under an explanation that is being generated for them.
    if (fix?.call) return prev;
    const fromFailedRun = fix !== null && fix.errors.some(error => error.source === 'query');
    if (fromFailedRun && fix.sql === sql) return prev;
    if (errors.length === 0) return fix === null ? prev : { ...prev, fix: null };
    // The same errors as before: keep whatever has been shown or dismissed for them.
    if (fix && !fromFailedRun && fixSignature(fix.sql, fix.errors) === fixSignature(sql, errors)) return prev;
    return { ...prev, fix: { errors, sql, call: null, result: null, dismissed: false } };
  });
}

/**
 * Records that a run of `sql` was rejected. Replaces whatever the validator had to say: this is the
 * database's own verdict on the query as actually executed, which outranks a static check of it.
 */
export function recordQueryFailure(id: string, sql: string, errors: SqlError[]): void {
  if (errors.length === 0) return;
  updateTabState(id, prev => {
    if (prev.fix?.call) return prev; // an explanation is already being generated - let it finish
    return { ...prev, fix: { errors, sql, call: null, result: null, dismissed: false } };
  });
}

/**
 * Starts a call of `kind` under `requestId`: the record of it a "Stop" button needs, with an empty
 * progress accumulator for the backend's events to be folded into.
 */
export function startCall(requestId: string, controller: AbortController, kind: CallKind): InFlightCall {
  return { requestId, controller, kind, startedAt: Date.now(), progress: EMPTY_PROGRESS };
}

export function useTabState(id: string): [TabState, (updater: (prev: TabState) => TabState) => void] {
  const idRef = useRef(id);
  const [state, setState] = useState<TabState>(() => loadTabState(id));

  // If the active SQL Lab tab changes, switch to that tab's own state here - during render, not
  // in an effect, so the still-stale previous tab's state is never actually painted even for one
  // frame (this component is a single long-lived instance for the whole SQL Lab session, so this
  // runs on every tab switch, not just defensively).
  if (idRef.current !== id) {
    idRef.current = id;
    setState(loadTabState(id));
  }

  useEffect(() => {
    const listener = (next: TabState) => setState(next);
    let set = listeners.get(id);
    if (!set) {
      set = new Set();
      listeners.set(id, set);
    }
    set.add(listener);
    // Pick up anything a background completion wrote for this tab between the render above and
    // this subscription taking effect.
    setState(loadTabState(id));
    return () => {
      set!.delete(listener);
      if (set!.size === 0) listeners.delete(id);
    };
  }, [id]);

  const update = useCallback((updater: (prev: TabState) => TabState) => updateTabState(idRef.current, updater), []);

  return [state, update];
}
