/**
 * Which theme the right-hand panel is drawn in - one choice that sets both its layout (see
 * `layouts/`) and whether the model and the panel write brief or full text (see `verbosity.ts`).
 * Each theme is a whole look to compare against the others, rather than a mix of separate switches.
 *
 * A display preference, so it lives in the browser's storage like the closed notices (`notices.ts`)
 * and costs nothing but the default when that storage is unavailable. Switched only from Change
 * theme in the panel header's ⋯ menu.
 */

import { useEffect, useState } from 'react';
import type { Verbosity } from './verbosity';

export interface ThemeDefinition {
  /** Unique, and kept: it is what the browser remembers and what feedback is filed under, so
   * renaming one splits its history in two. */
  id: string;
  /** In the header's Change theme menu. */
  name: string;
  /** What is different about it, in a sentence - the menu item's tooltip. */
  description: string;
  /** The id of the layout it draws the panel in (see `layouts/index.ts`). */
  layout: string;
  verbosity: Verbosity;
}

/** Every theme, in the order the header's Change theme menu lists them - the first is the
 * default. */
export const THEMES: ThemeDefinition[] = [
  {
    id: 'alpha',
    name: 'Alpha',
    description: "Ask, Accelerate and Fix as tabs, with full descriptions and explanations and the panel's full help text.",
    layout: 'tabs',
    verbosity: 'full',
  },
  {
    id: 'bravo',
    name: 'Bravo',
    description: 'Coming soon - a placeholder for now.',
    layout: 'coming-soon',
    verbosity: 'full',
  },
  {
    id: 'charlie',
    name: 'Charlie',
    description: 'Coming soon - a placeholder for now.',
    layout: 'coming-soon',
    verbosity: 'full',
  },
];

const STORAGE_KEY = 'waxwing.nltosqlandback.theme';

function find(id: string | null): ThemeDefinition | undefined {
  return THEMES.find(theme => theme.id === id);
}

function stored(): ThemeDefinition {
  try {
    // A theme that has since been removed (or renamed) falls back to the default.
    return find(window.localStorage.getItem(STORAGE_KEY)) ?? THEMES[0];
  } catch {
    return THEMES[0];
  }
}

let current: ThemeDefinition = stored();
const listeners = new Set<(theme: ThemeDefinition) => void>();

export function setPanelTheme(id: string): void {
  current = find(id) ?? THEMES[0];
  try {
    window.localStorage.setItem(STORAGE_KEY, current.id);
  } catch {
    // It still switches; it just won't be remembered.
  }
  listeners.forEach(listener => listener(current));
}

export function getPanelTheme(): ThemeDefinition {
  return current;
}

/** The theme now, kept current. */
export function usePanelTheme(): ThemeDefinition {
  const [theme, setTheme] = useState(current);
  useEffect(() => {
    listeners.add(setTheme);
    setTheme(current);
    return () => {
      listeners.delete(setTheme);
    };
  }, []);
  return theme;
}
