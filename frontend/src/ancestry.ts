/**
 * What the SQL in a SQL Lab tab used to be, before the panel changed it - its "ancestors" - so the
 * "Compare with original" tab under the editor (see `ComparePane`, `compareView`) can diff the query
 * the user started from against the one they have now, with a note on each change (see
 * `changeNotes`). Every change the panel puts in the editor to SQL that was already there is
 * recorded: a refinement, an update from Ask, an applied fix, an acceleration. SQL written from
 * scratch isn't - it changed nothing.
 *
 * Per SQL Lab tab, oldest first: `versions[0]` is the original, the last is the most recent version
 * the panel put there. A change to SQL that has been edited by hand since records the edited SQL as
 * a version of its own, so every step stays comparable; a change made after undoing back to an
 * earlier version carries on from that version, and the versions after it are dropped, as undo's
 * own redo steps would be. Opening a faster version in a new tab gives the new tab the source tab's
 * history plus that version.
 *
 * Kept apart from `TabState` for two reasons: "Start over" clears the panel's state for a tab but
 * not what the editor's SQL came from, and this has to survive a page reload - it lives in the
 * browser's storage, keyed by tab id. A tab saved on the server keeps its id across reloads, but a
 * new tab's id changes at its first reload (see `adoptAncestry`, which finds its history again).
 * Losing it (another browser, cleared storage) only loses the comparison. Dropped when the tab
 * closes.
 */

import { useEffect, useState } from 'react';
import type { RewriteChange } from './performanceResult';

export interface SqlVersion {
  sql: string;
  /** "Original", "Edited" (by hand, between two of the panel's changes), or what the panel did:
   * "Refined", "Updated", "Fixed", "Accelerated". */
  label: string;
  /** When it was recorded (epoch ms). */
  at: number;
  /** For a version the panel made, the changes that made it from the version before - shown as
   * notes in the comparison (see `changeNotes.ts`). Absent on the others, and on versions recorded
   * before changes carried their SQL. */
  changes?: RewriteChange[];
}

export interface Ancestry {
  versions: SqlVersion[];
}

const STORAGE_KEY = 'waxwing.nltosqlandback.ancestry';
/** Tabs remembered at most - the least recently changed go first. */
const MAX_TABS = 30;
/** Versions kept per tab: the original, then the latest ones. */
const MAX_VERSIONS = 10;

function load(): Map<string, Ancestry> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, Ancestry>) : {};
    return new Map(
      Object.entries(parsed).filter(([, value]) => Array.isArray(value?.versions) && value.versions.length >= 2),
    );
  } catch {
    return new Map();
  }
}

const store = load();
const listeners = new Set<() => void>();

function changed(): void {
  try {
    const newest = Array.from(store.entries())
      .sort(([, a], [, b]) => latest(b).at - latest(a).at)
      .slice(0, MAX_TABS);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(newest)));
  } catch {
    // Storage full or unavailable: it is still remembered for this page.
  }
  listeners.forEach(listener => listener());
}

function latest(ancestry: Ancestry): SqlVersion {
  return ancestry.versions[ancestry.versions.length - 1];
}

const same = (a: string, b: string) => a.trim() === b.trim();

export function getAncestry(tabId: string): Ancestry | null {
  return store.get(tabId) ?? null;
}

/** What the panel did to make a version. */
export type ChangeKind = 'Refined' | 'Updated' | 'Fixed' | 'Accelerated';

/**
 * Records that `tabId` now holds `newSql`, made from `fromSql` by `changes`, carrying on from
 * `base` (the history `fromSql` itself has - this tab's own, or the tab a new one was opened from).
 * Nothing is recorded for SQL that didn't change, or for a change to an empty editor.
 */
export function recordChange(
  tabId: string,
  kind: ChangeKind,
  fromSql: string,
  newSql: string,
  base: Ancestry | null,
  changes: RewriteChange[] | undefined,
): void {
  if (!fromSql.trim() || same(fromSql, newSql)) return;
  let versions = [...(base?.versions ?? [])];
  const now = Date.now();
  // Carrying on from an earlier version (after undo, say) drops the ones after it, as undo would.
  let from = -1;
  versions.forEach((version, index) => {
    if (same(version.sql, fromSql)) from = index;
  });
  if (from >= 0) versions = versions.slice(0, from + 1);
  else versions.push({ sql: fromSql, label: versions.length === 0 ? 'Original' : 'Edited', at: now });
  versions.push({ sql: newSql, label: kind, at: now, changes: changes ?? [] });
  const kept = versions.length > MAX_VERSIONS ? [versions[0], ...versions.slice(-(MAX_VERSIONS - 1))] : versions;
  store.set(tabId, { versions: kept });
  changed();
}

/** Superset's id for a tab saved on the server: digits. A new tab's temporary id never is. */
const PERMANENT_ID = /^\d+$/;

/**
 * Moves a history recorded under a tab's temporary id over to `tabId`, the id the same tab has now.
 * Returns whether it did.
 *
 * Superset gives a new SQL Lab tab a temporary id, and that is the id the tab keeps for the rest of
 * the page session, even once it is saved on the server. After a reload, though, every tab is
 * rebuilt from the server under its permanent id. A history recorded before the reload is then
 * stored under an id no tab has any more. The extension API offers no link between the two ids, so
 * the tab is recognised by what it holds instead: when `tabId` has no history of its own, `sql` is
 * one of the versions of a history kept under a temporary id that no open tab has (`openTabIds`).
 * A history whose latest version is `sql` is preferred to one that merely contains it. Histories
 * under permanent ids never move: those ids don't change.
 */
export function adoptAncestry(tabId: string, sql: string, openTabIds: string[]): boolean {
  if (store.has(tabId) || !sql.trim()) return false;
  const open = new Set(openTabIds);
  const orphans: [string, Ancestry][] = [];
  store.forEach((ancestry, key) => {
    if (!PERMANENT_ID.test(key) && !open.has(key) && key !== tabId) orphans.push([key, ancestry]);
  });
  const found =
    orphans.find(([, ancestry]) => same(latest(ancestry).sql, sql)) ??
    orphans.find(([, ancestry]) => ancestry.versions.some(version => same(version.sql, sql)));
  if (!found) return false;
  store.delete(found[0]);
  store.set(tabId, found[1]);
  changed();
  return true;
}

export function forgetAncestry(tabId: string): void {
  if (store.delete(tabId)) changed();
}

/**
 * Whether the SQL `sql` in tab `tabId` has an ancestor to compare against: the tab has a history,
 * and holds SQL that isn't simply the original again (after undoing the change, say).
 */
export function hasAncestor(tabId: string, sql: string): boolean {
  const ancestry = store.get(tabId);
  const trimmed = sql.trim();
  return ancestry !== undefined && trimmed !== '' && trimmed !== 'SELECT ...' && !same(ancestry.versions[0].sql, sql);
}

export function subscribeAncestry(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useAncestry(tabId: string | undefined): Ancestry | null {
  const [, setTick] = useState(0);
  useEffect(() => subscribeAncestry(() => setTick(tick => tick + 1)), []);
  return tabId ? getAncestry(tabId) : null;
}
