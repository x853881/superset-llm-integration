import { useCallback, useEffect, useRef, useState } from 'react';
import { sqlLab } from '@apache-superset/core';
import type { RequestContext } from './api';
import { CallProgress, perFrame } from './progress';
import { TabState, updateTabState } from './tabState';

/*
 * The SQL Lab plumbing every view of this extension needs - the right-hand panel and the two tabs
 * under the editor alike: which tab is active, its editor and SQL, what it points at, and where a
 * call's progress lands. One SQL Lab tab's state is shared by all of them (see `tabState.ts`).
 */

let requestIdCounter = 0;
/** A per-call id threaded through both the client-side `AbortController` and the backend's own
 * cancellation registry (see `api.cancelRequest`), so a "Stop" click can reach both. */
export function makeRequestId(): string {
  requestIdCounter += 1;
  return `${Date.now().toString(36)}-${requestIdCounter}-${Math.random().toString(36).slice(2)}`;
}

/** What a call made for `tab` tells the backend about it - captured before the call's first
 * `await`, like the tab id itself (see `tabState.ts`). */
export function contextFor(tab: sqlLab.Tab): RequestContext {
  return {
    databaseId: tab.databaseId ?? null,
    catalog: tab.catalog ?? null,
    tabId: tab.id,
  };
}

/**
 * Tracks whichever SQL Lab tab is currently active, updating as the user switches tabs - which,
 * in SQL Lab's own Redux store, happens three different ways: explicitly switching to an
 * already-open tab (`SET_ACTIVE_QUERY_EDITOR`, the only action `onDidChangeActiveTab` itself
 * listens for), creating a new tab (`ADD_QUERY_EDITOR`, whose reducer appends straight onto
 * `tabHistory` without a separate active-tab dispatch), and closing the active tab
 * (`REMOVE_QUERY_EDITOR`, same thing in reverse). All three need to be watched here, or this
 * misses two of the three ways the active tab can change. Re-reads the live active tab via
 * `getCurrentTab()` on every one of them rather than trusting an event's own payload -
 * `onDidCloseTab`'s payload is the tab that just closed, not whatever became active in its place.
 *
 * The database and schema events are watched for the same reason the panel shows them at all (see
 * `ContextBar`): they're part of the tab, so changing either has to re-read the tab here, or the
 * panel goes on claiming the question will be answered against the previous one. Those two are
 * tab-scoped events, though - the host binds them to whichever tab was active when the listener
 * was registered, which for a panel that subscribes once on mount is only ever the first one - so
 * they're a prompt to re-render, not the whole story; see `useCurrentTabContext`.
 */
function sameTab(a: sqlLab.Tab | undefined, b: sqlLab.Tab | undefined): boolean {
  if (!a || !b) return a === b;
  return a.id === b.id && a.databaseId === b.databaseId && a.catalog === b.catalog;
}

export function useActiveSqlLabTab(): sqlLab.Tab | undefined {
  const [tab, setTab] = useState<sqlLab.Tab | undefined>(() => sqlLab.getCurrentTab());
  useEffect(() => {
    const refresh = () => setTab(sqlLab.getCurrentTab());
    const disposables = [
      sqlLab.onDidChangeActiveTab(refresh),
      // Set directly from the event's own payload rather than re-reading `getCurrentTab()`: a
      // freshly created tab is always about to become the active one, and re-reading the host's
      // store here can momentarily still answer with whichever tab was active before it, which
      // would otherwise show the previous tab's state for a render or two. (From sql-helper.)
      sqlLab.onDidCreateTab(newTab => setTab(newTab)),
      sqlLab.onDidCloseTab(refresh),
      sqlLab.onDidChangeEditorDatabase(refresh),
      sqlLab.onDidChangeEditorSchema(refresh),
    ];
    // Pick up any change that happened between the initial read above and this subscription.
    refresh();
    // And a fallback for the switch no event reports: the first visit to a tab after the page
    // loads. SQL Lab loads a tab's details lazily, and until they arrive the host can't build a
    // `Tab` for it, so `onDidChangeActiveTab` stays silent for that switch (Superset 6.1's
    // `getTab` returns undefined, and the listener is skipped). Cheap: a store read, and a
    // re-render only when something about the active tab actually changed.
    const poll = window.setInterval(() => {
      const current = sqlLab.getCurrentTab();
      setTab(previous => (sameTab(previous, current) ? previous : current));
    }, 750);
    return () => {
      window.clearInterval(poll);
      disposables.forEach(disposable => disposable.dispose());
    };
  }, []);
  return tab;
}

/**
 * Resolves `tab`'s code editor (a proxy that keeps working across an Ace<->Monaco swap) and keeps
 * a live copy of its SQL text in sync via `onDidChangeContent`, re-resolving whenever `tab`
 * itself changes. `editor` is null until it resolves (or there's no active tab at all) - callers
 * should treat that as "not ready yet" rather than an error.
 *
 * The editor and its text are one piece of state rather than two, so no render ever sees a resolved
 * editor alongside another tab's text - or, worse, alongside the empty string that stands for "not
 * read yet". React 17 doesn't batch state updates made from a promise callback, so two `useState`s
 * set one after the other in `getEditor().then(...)` would paint exactly that in between, and
 * anything keyed off "the SQL is empty" (see `useValidatorErrors`) would act on it.
 */
export function useTabEditor(tab: sqlLab.Tab | undefined): {
  editor: sqlLab.Editor | null;
  sql: string;
  resync: () => void;
} {
  const tabIdRef = useRef(tab?.id);
  const [resolved, setResolved] = useState<{ editor: sqlLab.Editor | null; sql: string }>({ editor: null, sql: '' });

  // Clears the previous tab's editor during render, not in the effect below - the same trick
  // `useTabState` uses, for the same reason. An effect-only reset would leave one render, right
  // after switching tabs, where this hook still returns the *previous* tab's editor alongside the
  // *new* tab's state - `pendingSql` included - and the `pendingSql` effect would then write the
  // new tab's SQL into the previous tab's editor and mark it applied. (From sql-helper.)
  let current = resolved;
  if (tabIdRef.current !== tab?.id) {
    tabIdRef.current = tab?.id;
    current = { editor: null, sql: '' };
    setResolved(current);
  }

  useEffect(() => {
    if (!tab) return undefined;
    let cancelled = false;
    let contentDisposable: { dispose(): void } | undefined;
    tab.getEditor().then(resolvedEditor => {
      if (cancelled) return;
      setResolved({ editor: resolvedEditor, sql: resolvedEditor.getValue() });
      contentDisposable = resolvedEditor.onDidChangeContent(e =>
        setResolved(prev => ({ ...prev, sql: e.getValue() })),
      );
    });
    return () => {
      cancelled = true;
      contentDisposable?.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab?.id]);

  // Re-reads the editor's text into `sql`. Needed after the panel itself writes to the editor: the
  // host's `onDidChangeContent` reports what the user types, but (in Superset 6.1, at least) not a
  // programmatic `setValue`, so without this `sql` would go on describing what was there before.
  const resync = useCallback(() => {
    setResolved(prev => {
      if (!prev.editor) return prev;
      try {
        return { ...prev, sql: prev.editor.getValue() };
      } catch {
        return prev;
      }
    });
  }, []);

  return { ...current, resync };
}

/**
 * The database and catalog the active tab points at, re-read on every render rather than
 * held in state. `useActiveSqlLabTab`'s snapshot can't be trusted for these: the host's
 * database/schema events are scoped to a single tab (see above), so a change made in any other
 * tab would leave that snapshot - and the readout drawn from it - quietly wrong about where the
 * question is about to be answered. This is a synchronous read of SQL Lab's own store, so the
 * worst case is that it's a render behind, not that it's showing a different tab's database.
 */
export function useCurrentTabContext(fallback: sqlLab.Tab | undefined): {
  databaseName: string | null;
  catalog: string | null;
} {
  const tab = sqlLab.getCurrentTab() ?? fallback;
  if (!tab) return { databaseName: null, catalog: null };
  const database = sqlLab.getDatabases().find(candidate => candidate.id === tab.databaseId);
  return { databaseName: database?.name ?? null, catalog: tab.catalog };
}

/**
 * Somewhere for a call's progress events to land: `place` puts a fresh `CallProgress` into whichever
 * slot of `targetId`'s state holds that call, and the result is written at most once per frame.
 *
 * Coalesced because the text arrives a token at a time - hundreds of events a second from a fast
 * provider - and each one would otherwise be its own React re-render. Every event is still
 * accumulated (see `progress.applyEvent`); this only limits how often the accumulation is painted.
 */
export function progressSink(
  targetId: string,
  place: (state: TabState, progress: CallProgress) => TabState,
): (progress: CallProgress) => void {
  return perFrame<CallProgress>(progress => updateTabState(targetId, prev => place(prev, progress)));
}

