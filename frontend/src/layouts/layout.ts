/**
 * What a layout of the side panel is, and what it is given - the whole contract for adding one.
 *
 * The panel works out everything itself (see `usePanelModel`): the calls, the undo timeline, each
 * SQL Lab tab's own state, and each of the four workflows drawn and ready to go on screen. A layout
 * only arranges them - as tabs, as one column, side by side, whatever is being tried - so it can't
 * break any of that, and the same work can be looked at in every layout by switching between them.
 *
 * To add one: a folder beside `tabs/` and `flow/` whose `index.tsx` default-exports
 * `defineLayout({...})`, and its line in `layouts/index.ts`. It then appears in the header's layout
 * menu, on the options page and in `LAYOUT_SHORTCUT`'s cycle, and its id goes out with every piece
 * of feedback (see `feedback.ts`), so it can be compared with the others in the telemetry.
 */

import type React from 'react';
import type { WorkflowSegment } from '../tabState';

/** One of the four workflows - Ask, Refine, Fix, Describe - ready to be put on screen. */
export interface WorkflowView {
  id: WorkflowSegment;
  /** Its name ("Ask") and what it does in a few words - for a tab, a heading, a tooltip. */
  label: string;
  hint: string;
  /** The workflow itself - its composer, progress, results and empty state - to draw as it is. */
  body: React.ReactNode;
  /**
   * Whether it has anything to act on now: Refine and Describe need SQL (or, for Describe, a
   * description already written), Fix needs something reported against the SQL or a correction
   * just applied. A layout that leaves out what has nothing to do uses this; one that shows every
   * workflow all the time can ignore it, since the body has its own empty state.
   */
  available: boolean;
  /**
   * Whether the SQL's state says this is where the action is: Ask while there is no SQL or a
   * question is being written or answered, Fix while problems are waiting or being explained,
   * Refine whenever there is SQL, Describe while one is being written. What a layout that opens
   * and closes workflows opens.
   */
  wantsAttention: boolean;
  /** A call of its own in flight - worth showing even while it is off screen. */
  running: boolean;
  /** A result arrived while the user was looking at another workflow. */
  unread: boolean;
  /** Problems waiting to be explained - only ever more than none on Fix. */
  count: number;
  /** It writes SQL from the tab's tables, so the schema-knowledge bar (`PanelModel.schemaBar`)
   * belongs beside it. */
  usesSchema: boolean;
}

/** The panel as it is now, for the active SQL Lab tab - what a layout is drawn from. */
export interface PanelModel {
  /** The active SQL Lab tab's id. Key anything the layout keeps for itself by it, so that it
   * belongs to that tab and doesn't carry over to the next (the single column's opened and closed
   * sections, say). */
  tabKey: string;
  workflows: Record<WorkflowSegment, WorkflowView>;
  /** The workflow last shown, kept per tab. The panel moves it too - undo stepping onto a fix,
   * "Edit in Ask", a suggested refinement - so a layout should follow it rather than keep its own. */
  focused: WorkflowSegment;
  focus: (id: WorkflowSegment) => void;
  /** The last call's failure, closable - null when there is none. Belongs where it is seen
   * whichever workflow is showing. */
  error: React.ReactNode;
  /** Whether the assistant knows the tab's tables (see `WorkflowView.usesSchema`). */
  schemaBar: React.ReactNode;
  /** The foot of the panel: the standing notes and, when `withComposer` and there is SQL, the
   * refine box. Together because the notes say something different above the box. */
  footer: (withComposer: boolean) => React.ReactNode;
}

export interface LayoutProps {
  model: PanelModel;
}

export interface LayoutDefinition {
  /** Unique, and kept: it is what the browser remembers as the chosen layout and what feedback
   * is filed under, so renaming one splits its history in two. */
  id: string;
  /** In the header's layout menu. */
  name: string;
  /** On the options page, under the name: what is different about it, in a sentence. */
  description: string;
  /** Everything under the header and the tab's database bar, the full height of the panel (a
   * column, so a fragment of rows is fine) - see `LayoutBody` for the scrolling part. */
  Component: React.ComponentType<LayoutProps>;
}

/** A layout, checked against `LayoutDefinition` - what each layout's `index.tsx` default-exports. */
export function defineLayout(definition: LayoutDefinition): LayoutDefinition {
  return definition;
}
