/**
 * Which layout the right-hand panel is drawn in - the same workflows and state, arranged
 * differently. The layouts themselves, and how to add one, are in `layouts/`; which one is drawn
 * is decided by the chosen theme (see `panelTheme.ts`), so this only looks it up.
 */

import { LAYOUTS, LayoutDefinition } from './layouts';
import { getPanelTheme, usePanelTheme } from './panelTheme';

/** A layout's id (see `LayoutDefinition.id`). */
export type PanelLayout = string;

function lookUp(id: PanelLayout): LayoutDefinition {
  // A theme naming a layout that has since been removed (or renamed) falls back to the default.
  return LAYOUTS.find(layout => layout.id === id) ?? LAYOUTS[0];
}

export function getPanelLayout(): PanelLayout {
  return lookUp(getPanelTheme().layout).id;
}

/** The chosen theme's layout, kept current. */
export function useLayout(): LayoutDefinition {
  return lookUp(usePanelTheme().layout);
}
