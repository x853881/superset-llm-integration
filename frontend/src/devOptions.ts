/**
 * The options page laid over the side panel (see `OptionsOverlay`) - a development aid for trying
 * the panel's variants against each other: which layout, brief or full prose, and whatever is added
 * next. Whether it is open is kept here rather than in the panel, so the shortcut and the ⋯ menu
 * can both open it.
 *
 * Summoned with `OPTIONS_SHORTCUT` from anywhere in SQL Lab - listened for in the capture phase,
 * like the layout shortcut (`panelLayout.ts`), so the SQL editor (whose own settings menu Ace binds
 * to the same keys) never sees it. Ctrl on a Mac too: Cmd+, is the browser's own settings.
 */

import { useEffect, useState } from 'react';

export const OPTIONS_SHORTCUT = 'Ctrl+,';

let open = false;
const listeners = new Set<(open: boolean) => void>();

export function setOptionsOpen(value: boolean): void {
  open = value;
  listeners.forEach(listener => listener(value));
}

export function isOptionsShortcut(event: KeyboardEvent): boolean {
  return event.ctrlKey && !event.shiftKey && !event.altKey && !event.metaKey && (event.code === 'Comma' || event.key === ',');
}

/** Whether the options page is open now, kept current - and its shortcut listened for (toggling it;
 * Escape closes it). */
export function useOptionsOpen(): boolean {
  const [value, setValue] = useState(open);
  useEffect(() => {
    listeners.add(setValue);
    setValue(open);
    const onKeyDown = (event: KeyboardEvent) => {
      if (isOptionsShortcut(event)) {
        event.preventDefault();
        event.stopPropagation();
        setOptionsOpen(!open);
      } else if (open && event.key === 'Escape') {
        event.stopPropagation();
        setOptionsOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      listeners.delete(setValue);
      window.removeEventListener('keydown', onKeyDown, true);
    };
  }, []);
  return value;
}
