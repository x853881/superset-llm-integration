/**
 * Feedback on the model's results: a thumbs up or down, and for a thumbs down, why. Shown under each
 * result (see `FeedbackBar`) and sent to the backend's `/feedback`, which logs it at its own
 * `FEEDBACK` level and files it as a span in the trace of the request that produced the result - so
 * whoever reads it sees the prompt and the reply it is about.
 *
 * Built to cost the person giving it as little as possible: either thumb is one click and is sent
 * at once; a thumbs down then offers the likeliest reasons for that kind of result, each one more
 * click; only "Other…" needs typing. Changing one's mind (a different thumb, a reason after the
 * thumb) sends again under the same `feedbackId`, and the latest record for an id is the one that
 * counts.
 *
 * What was given is kept for the page's lifetime, by the result's span id, so it survives switching
 * tabs and layouts and undoing back to an earlier result.
 */

import { useEffect, useState } from 'react';
import { sendFeedback } from './api';
import { getPanelLayout } from './panelLayout';
import type { TraceRef } from './progress';
import { getVerbosity } from './verbosity';

/** What kind of result is being rated - the reasons offered depend on it. */
export type FeedbackTarget = 'sql' | 'sql-failure' | 'fix' | 'accelerate' | 'explain-plan' | 'describe' | 'breakdown';

export interface Reason {
  id: string;
  label: string;
}

/** Too long under Full, too short under Brief - the length complaint each setting is likeliest to get. */
function lengthReason(brief: boolean): Reason {
  return brief ? { id: 'too-short', label: 'Too short' } : { id: 'too-long', label: 'Too long' };
}

/** The likeliest reasons for a thumbs down on each kind of result, likeliest first. */
export function reasonsFor(target: FeedbackTarget, brief: boolean): Reason[] {
  switch (target) {
    case 'sql':
      return [
        { id: 'misread', label: 'Misread the question' },
        { id: 'wrong-tables', label: 'Wrong tables or columns' },
        { id: 'wrong-results', label: 'Wrong results' },
        { id: 'doesnt-run', label: "Doesn't run" },
      ];
    case 'sql-failure':
      return [
        { id: 'should-answer', label: 'It should have answered' },
        { id: 'unhelpful', label: "The explanation didn't help" },
      ];
    case 'fix':
      return [
        { id: 'not-fixed', label: "Didn't fix it" },
        { id: 'wrong-explanation', label: 'Wrong explanation' },
        { id: 'changed-too-much', label: 'Changed too much' },
      ];
    case 'accelerate':
      return [
        { id: 'not-faster', label: 'Not faster' },
        { id: 'changes-results', label: 'Changes the results' },
        { id: 'doesnt-run', label: "Doesn't run" },
        { id: 'too-complicated', label: 'Too complicated' },
      ];
    case 'explain-plan':
      return [{ id: 'misreads-plan', label: 'Misreads the plan' }, { id: 'too-technical', label: 'Too technical' }, lengthReason(brief)];
    case 'describe':
      return [{ id: 'wrong', label: 'Gets the query wrong' }, { id: 'too-vague', label: 'Too vague' }, lengthReason(brief)];
    case 'breakdown':
      return [
        { id: 'wrong', label: 'Gets the query wrong' },
        { id: 'bad-sections', label: 'Sections split badly' },
        { id: 'too-technical', label: 'Too technical' },
        lengthReason(brief),
      ];
    default:
      return [];
  }
}

/** One person's say on one result. */
export interface FeedbackChange {
  rating: 'up' | 'down';
  /** An offered reason's id, or "other" with `comment`. */
  reason?: string;
  label?: string;
  comment?: string;
}

export interface GivenFeedback extends FeedbackChange {
  feedbackId: string;
  status: 'sending' | 'sent' | 'failed';
}

const given = new Map<string, GivenFeedback>();
const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach(listener => listener());
}

function newFeedbackId(): string {
  return `fb-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Records `change` against the result `trace` identifies - shown at once, sent in the background. */
export function giveFeedback(trace: TraceRef, target: FeedbackTarget, change: FeedbackChange): void {
  const entry: GivenFeedback = {
    feedbackId: given.get(trace.spanId)?.feedbackId ?? newFeedbackId(),
    ...change,
    status: 'sending',
  };
  given.set(trace.spanId, entry);
  notify();
  const settle = (status: GivenFeedback['status']) => {
    // A later change has already replaced this one: that one's outcome is what to show.
    if (given.get(trace.spanId) !== entry) return;
    given.set(trace.spanId, { ...entry, status });
    notify();
  };
  sendFeedback({
    traceId: trace.traceId,
    spanId: trace.spanId,
    feedbackId: entry.feedbackId,
    target,
    ...change,
    // Which variant this is feedback on - the A/B options (see `devOptions.ts`).
    variant: { layout: getPanelLayout(), verbosity: getVerbosity() },
  }).then(
    () => settle('sent'),
    () => settle('failed'),
  );
}

/** What has been said about the result `trace` identifies, kept current. */
export function useFeedback(trace: TraceRef | undefined): GivenFeedback | undefined {
  const key = trace?.spanId;
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion(version => version + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return key ? given.get(key) : undefined;
}
