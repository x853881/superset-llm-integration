/**
 * Every layout the side panel can be drawn in, in the order the header's menu and the options page
 * list them - the first is the default. A new layout is its own folder here and one line below; see
 * `layout.ts` for what a layout is and what it is given.
 */

import type { LayoutDefinition } from './layout';
import tabs from './tabs';
import flow from './flow';

export type { LayoutDefinition, LayoutProps, PanelModel, WorkflowView } from './layout';
export { defineLayout } from './layout';

export const LAYOUTS: LayoutDefinition[] = withUniqueIds([tabs, flow]);

/** Leaves out any layout whose id an earlier one already has, and says so: two under one id would
 * share a remembered choice and a history in the telemetry, and only one could ever be picked. */
function withUniqueIds(layouts: LayoutDefinition[]): LayoutDefinition[] {
  const seen = new Set<string>();
  return layouts.filter(layout => {
    if (seen.has(layout.id)) {
      console.error(`Two panel layouts have the id "${layout.id}"; only the first is offered.`);
      return false;
    }
    seen.add(layout.id);
    return true;
  });
}
