import React from 'react';
import { sqlLab, views } from '@apache-superset/core';
import { adoptAncestry, forgetAncestry, hasAncestor, subscribeAncestry } from './ancestry';
import { ComparePane } from './components/ComparePane';

const VIEW = { id: 'waxwing.nltosqlandback.compare', name: 'Compare with original' };

/** How often the active tab and its SQL are looked at - for what no event reports (see below). */
const POLL_MS = 750;

let registration: { dispose(): void } | null = null;
let syncing = false;
let again = false;

/** The active tab's SQL, or '' when there's no tab or its editor can't be read. */
async function activeSql(): Promise<{ id: string; sql: string } | null> {
  const tab = sqlLab.getCurrentTab();
  if (!tab) return null;
  try {
    const editor = await tab.getEditor();
    return { id: tab.id, sql: editor.getValue() };
  } catch {
    return { id: tab.id, sql: '' };
  }
}

/** The ids of the SQL Lab tabs open now - those whose details have loaded, at least. */
function openTabIds(): string[] {
  try {
    return sqlLab.getTabs().map(tab => tab.id);
  } catch {
    return [];
  }
}

/**
 * Makes SQL Lab redraw its row of tabs under the editor. The host reads the registered views only
 * when that row re-renders, and nothing about registering one makes it; switching the active tab
 * there does, so this switches away and straight back - both before the browser paints, so nothing
 * is seen to move.
 */
async function redrawPanels(): Promise<void> {
  try {
    const current = sqlLab.getActivePanel()?.id ?? 'Results';
    await sqlLab.setActivePanel(current === 'Results' ? 'History' : 'Results');
    await sqlLab.setActivePanel(current);
  } catch {
    // No SQL Lab on screen: it reads the views afresh when it next draws.
  }
}

/** Registers the "Compare with original" tab while the active tab's SQL has an ancestor, and
 * removes it again when it doesn't - moving off it first if it's the one showing. */
async function sync(): Promise<void> {
  if (syncing) {
    again = true;
    return;
  }
  syncing = true;
  try {
    do {
      again = false;
      const active = await activeSql();
      // A history recorded under the tab's temporary id before a reload finds it again here.
      if (active !== null) adoptAncestry(active.id, active.sql, openTabIds());
      const wanted = active !== null && hasAncestor(active.id, active.sql);
      if (wanted && !registration) {
        registration = views.registerView(VIEW, 'sqllab.panels', () => <ComparePane />);
        await redrawPanels();
      } else if (!wanted && registration) {
        try {
          if (sqlLab.getActivePanel()?.id === VIEW.id) await sqlLab.setActivePanel('Results');
        } catch {
          // Nothing showing to move off.
        }
        registration.dispose();
        registration = null;
        await redrawPanels();
      }
    } while (again);
  } finally {
    syncing = false;
  }
}

/**
 * Keeps the "Compare with original" tab under the editor in step with the active SQL Lab tab: there
 * while that tab's SQL descends from SQL the panel changed (see `ancestry`), gone otherwise -
 * not a tab that is always there and mostly says it has nothing to compare.
 *
 * Watched from here rather than from a component, since none of this extension's views is sure to
 * be mounted. Tab switches and closes are events; the SQL changing back to the original (an undo,
 * say) and the first visit to a tab after a reload (which the host doesn't report - see
 * `useActiveSqlLabTab`) aren't, so they're caught by a light poll.
 */
export function watchCompareView(): void {
  subscribeAncestry(() => void sync());
  sqlLab.onDidChangeActiveTab(() => void sync());
  sqlLab.onDidCloseTab(tab => {
    forgetAncestry(tab.id);
    void sync();
  });
  window.setInterval(() => void sync(), POLL_MS);
  void sync();
}
