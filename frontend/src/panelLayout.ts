/**
 * Which layout the right-hand panel is drawn in - the same workflows and state, arranged
 * differently, so layouts can be tried side by side on the same work and switched between at will.
 * The layouts themselves, and how to add one, are in `layouts/`; this keeps which one is chosen.
 *
 * A display preference, so it lives in the browser's storage like the closed notices (`notices.ts`)
 * and costs nothing but the default when that storage is unavailable. Switched from the panel
 * header's menu, or with `LAYOUT_SHORTCUT` - not Ctrl+Tab, which browsers keep for their own tabs
 * and never hand to the page.
 */

import { useEffect, useState } from 'react';
import { LAYOUTS, LayoutDefinition } from './layouts';

/** A layout's id (see `LayoutDefinition.id`). */
export type PanelLayout = string;

export const LAYOUT_SHORTCUT = 'Ctrl+Shift+L';

const STORAGE_KEY = 'waxwing.nltosqlandback.layout';

function stored(): PanelLayout {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    // A layout that has since been removed (or renamed) falls back to the default.
    return value !== null && LAYOUTS.some(layout => layout.id === value) ? value : LAYOUTS[0].id;
  } catch {
    return LAYOUTS[0].id;
  }
}

let current: PanelLayout = stored();
const listeners = new Set<(layout: PanelLayout) => void>();

export function setPanelLayout(layout: PanelLayout): void {
  current = layout;
  try {
    window.localStorage.setItem(STORAGE_KEY, layout);
  } catch {
    // It still switches; it just won't be remembered.
  }
  listeners.forEach(listener => listener(layout));
}

export function getPanelLayout(): PanelLayout {
  return current;
}

/** The next layout along, wrapping round - what the shortcut steps through. */
export function nextPanelLayout(): void {
  const index = LAYOUTS.findIndex(layout => layout.id === current);
  setPanelLayout(LAYOUTS[(index + 1) % LAYOUTS.length].id);
}

export function isLayoutShortcut(event: KeyboardEvent): boolean {
  return event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey && event.code === 'KeyL';
}

/** The layout now, kept current. */
export function usePanelLayout(): PanelLayout {
  const [layout, setLayout] = useState(current);
  useEffect(() => {
    listeners.add(setLayout);
    setLayout(current);
    return () => {
      listeners.delete(setLayout);
    };
  }, []);
  return layout;
}

/** The chosen layout itself, kept current. */
export function useLayout(): LayoutDefinition {
  const id = usePanelLayout();
  return LAYOUTS.find(layout => layout.id === id) ?? LAYOUTS[0];
}

/**
 * Steps to the next layout on `LAYOUT_SHORTCUT`, wherever the focus is in SQL Lab - listened for in
 * the capture phase so the SQL editor, which would otherwise take the keystroke, never sees it.
 */
export function useLayoutShortcut(): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isLayoutShortcut(event)) return;
      event.preventDefault();
      event.stopPropagation();
      nextPanelLayout();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, []);
}
