import { sqlLab } from '@apache-superset/core';
import { describeBreakdown, isCancellation } from './api';
import type { CallProgress } from './progress';
import { contextFor, makeRequestId, progressSink } from './sqlLabHooks';
import { isSqlEmpty, markUnread, rememberBreakdown, startCall, TabState, updateTabState } from './tabState';

const intoNlCall = (state: TabState, progress: CallProgress): TabState =>
  state.nlCall ? { ...state, nlCall: { ...state.nlCall, progress } } : state;

/** The editor's SQL as a breakdown sees it - trailing whitespace makes no difference to a query. */
export function breakdownSource(sql: string): string {
  return sql.replace(/\s+$/, '');
}

/**
 * "Break it down": the section-by-section description of `sql`, kept in `tab`'s state where both
 * the Describe tab of the side panel and the "Break it down" tab under the editor show it. Shares
 * the describe workflow's call slot, so only one description of a tab runs at a time whichever view
 * started it. With `formatSql` the backend lays the query out one clause per line first, and the
 * sections refer to that layout (`result.sql`), not the editor's text.
 */
export async function runBreakdown(tab: sqlLab.Tab, sql: string, formatSql: boolean, busy: boolean): Promise<void> {
  const sqlAtStart = breakdownSource(sql);
  if (isSqlEmpty(sqlAtStart) || busy) return;
  const targetId = tab.id;
  const context = contextFor(tab);
  const controller = new AbortController();
  const requestId = makeRequestId();
  const sink = progressSink(targetId, intoNlCall);
  updateTabState(targetId, prev => ({
    ...prev,
    generatingNl: true,
    error: '',
    nlCall: startCall(requestId, controller, 'breakdown'),
  }));
  try {
    const result = await describeBreakdown(sqlAtStart, context, requestId, sink, controller.signal, formatSql);
    updateTabState(targetId, prev => rememberBreakdown(prev, { result, sql: sqlAtStart }));
  } catch (e) {
    if (!isCancellation(e)) {
      updateTabState(targetId, prev => ({
        ...prev,
        error: e instanceof Error ? e.message : 'Failed to break the query down',
      }));
    }
  } finally {
    updateTabState(targetId, prev => ({ ...prev, generatingNl: false, nlCall: null }));
    markUnread(targetId, 'describe');
  }
}
