/**
 * What the backend reports about a call while it's still running, and how the panel accumulates it.
 *
 * Every LLM-backed endpoint answers with a server-sent event stream rather than one JSON body (see
 * `backend/src/waxwing/nltosqlandback/api.py`), for two reasons the panel makes visible: the
 * pipeline says which stage it has reached and what that stage found, and the model's answer
 * arrives as it's written rather than all at once at the end. Against a local model a call runs to
 * tens of seconds, which is far too long to show nothing but a spinner.
 *
 * `applyEvent` folds those events into the `CallProgress` the progress display renders - kept in
 * the acting tab's own state, so switching tabs mid-call and coming back shows the call still
 * running, at the stage it has actually reached.
 */

export type StageStatus = 'pending' | 'active' | 'done' | 'failed';

export interface ProgressStage {
  /** Identifies the stage - a repeat of the same key updates that stage rather than adding one. */
  key: string;
  label: string;
  /** What this stage is working on, or - once it's finished - what it found. */
  detail: string | null;
  status: StageStatus;
  /** How long it took, once it's finished. */
  seconds: number | null;
  /** What this step was called in the plan, before it had run. A step names itself differently
   * depending on its state ("Drafting SQL" / "Drafted the SQL"), so putting one back to pending
   * has to put its name back too. */
  plannedLabel?: string;
}

/** The text of one field of the result, as far as the model has written it. */
export type StreamedText = Record<string, string>;

/** Where a reasoning model's thinking is streamed - see the backend's `progress.REASONING_FIELD`.
 * Not a field of any result: it's how the answer was arrived at, not part of it. */
export const REASONING_FIELD = 'reasoning';

export interface CallProgress {
  stages: ProgressStage[];
  text: StreamedText;
  /** Parts of the result already settled, by name, before the result itself arrives - the SQL a
   * breakdown describes, its sections so far (see the backend's `progress.partial`). */
  partial?: Record<string, unknown>;
}

export const EMPTY_PROGRESS: CallProgress = { stages: [], text: {} };

/** Which request produced a result - its trace and span ids, as the backend sends them with the
 * result. What feedback on the result is filed under (see `feedback.ts`), so the feedback lands in
 * the same trace as the prompt and the model's reply. */
export interface TraceRef {
  traceId: string;
  spanId: string;
}

/** A result that knows the request it came from - set by `api.ts` as the result arrives. */
export interface Traced {
  trace?: TraceRef;
}

export type StreamEvent =
  | { type: 'plan'; stages: { key: string; label: string }[] }
  | ({ type: 'stage' } & ProgressStage)
  | { type: 'reset'; field: string }
  | { type: 'delta'; field: string; text: string }
  | { type: 'partial'; field: string; value: unknown }
  | { type: 'result'; result: unknown; trace?: TraceRef }
  | { type: 'error'; message: string }
  | { type: 'cancelled' };

/**
 * `progress` with `event` folded in.
 *
 * A plan lays out every step the call intends to take, so the panel can show the work still to come
 * rather than a list that grows from nothing. After that, a stage key already present is updated in
 * place (which is how a stage reports its own outcome and its duration); a key not in the plan is
 * appended; and any stage still running when a *different* one starts is marked done, since the
 * backend only moves on once the previous step has finished.
 */
export function applyEvent(progress: CallProgress, event: StreamEvent): CallProgress {
  if (event.type === 'plan') {
    return {
      ...progress,
      stages: event.stages.map(stage => ({
        ...stage,
        detail: null,
        status: 'pending',
        seconds: null,
        plannedLabel: stage.label,
      })),
    };
  }
  if (event.type === 'stage') {
    const { key, label, detail, status, seconds } = event;
    const existing = progress.stages.findIndex(stage => stage.key === key);
    if (existing >= 0) {
      const next = { ...progress.stages[existing], label, detail, status, seconds: seconds ?? null };
      const stages = progress.stages.map((stage, index) =>
        index === existing ? next : index > existing && status === 'active' ? awaitingAgain(stage) : stage,
      );
      return { ...progress, stages };
    }
    const next = { key, label, detail, status, seconds: seconds ?? null };
    const settled = progress.stages.map(stage =>
      stage.status === 'active' ? { ...stage, status: 'done' as StageStatus } : stage,
    );
    return { ...progress, stages: [...settled, next] };
  }
  if (event.type === 'reset') {
    return { ...progress, text: { ...progress.text, [event.field]: '' } };
  }
  if (event.type === 'partial') {
    return { ...progress, partial: { ...progress.partial, [event.field]: event.value } };
  }
  if (event.type === 'delta') {
    return { ...progress, text: { ...progress.text, [event.field]: (progress.text[event.field] ?? '') + event.text } };
  }
  return progress;
}

/**
 * A step that had already finished, put back to pending because an earlier step has started again.
 *
 * Only happens on a retry - a draft that didn't parse is redrafted, and the steps that ran after it
 * are about to run again. Their previous outcome is no longer true of this attempt, and leaving
 * "Parsed cleanly, 0.4s" on screen under a step that is being redone would be a lie.
 */
function awaitingAgain(stage: ProgressStage): ProgressStage {
  if (stage.status !== 'done' && stage.status !== 'failed') return stage;
  return { ...stage, status: 'pending', detail: null, seconds: null, label: stage.plannedLabel ?? stage.label };
}

/**
 * `apply`, but called at most once per animation frame with only the latest value it was handed.
 *
 * A token stream can fire hundreds of events a second, and each one would otherwise be its own
 * React state write and re-render. The events themselves are still all accumulated - this only
 * coalesces how often the result of accumulating them is painted. A frame that never comes (a
 * backgrounded browser tab) simply means the last value is painted when the tab is looked at
 * again; nothing is waiting on it, since the call's own result is what the panel acts on.
 */
export function perFrame<T>(apply: (value: T) => void): (value: T) => void {
  let pending: { value: T } | null = null;
  let frame: number | null = null;
  return (value: T) => {
    pending = { value };
    if (frame !== null) return;
    frame = window.requestAnimationFrame(() => {
      frame = null;
      const next = pending;
      pending = null;
      if (next) apply(next.value);
    });
  };
}
