/**
 * Remembers which of the panel's standing notices a user has closed.
 *
 * The notices that explain how the panel behaves ("a refinement rewrites the SQL in the editor and
 * waits for Run") are worth reading once and are in the way from then on, so they're closeable -
 * and stay closed, which is the part that needs somewhere to record it. There's nowhere
 * per-user-per-instance to put that in the extension API, so it goes in the browser's own storage,
 * keyed by notice: it's a display preference, so losing it (a different browser, cleared storage)
 * costs nothing but showing the notice again.
 *
 * Not used for the warning that AI can make mistakes and the user is responsible for what they run
 * (see `ResponsibilityNote`), which is not closeable at all.
 */

import { useCallback, useState } from 'react';

const STORAGE_PREFIX = 'waxwing.nltosqlandback.dismissed.';

function isDismissed(id: string): boolean {
  try {
    return window.localStorage.getItem(`${STORAGE_PREFIX}${id}`) === 'true';
  } catch {
    // Storage can be unavailable outright (private windows, blocked site data). A notice that
    // can't remember being closed is a much smaller problem than a panel that won't render.
    return false;
  }
}

function remember(id: string): void {
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${id}`, 'true');
  } catch {
    // Same again: closing it still works for this session, it just won't stick.
  }
}

/** Whether the notice `id` should still be shown, and the callback to close it for good. */
export function useDismissible(id: string): [boolean, () => void] {
  const [dismissed, setDismissed] = useState(() => isDismissed(id));
  const dismiss = useCallback(() => {
    remember(id);
    setDismissed(true);
  }, [id]);
  return [!dismissed, dismiss];
}
