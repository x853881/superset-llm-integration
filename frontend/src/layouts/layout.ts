/**
 * What a layout of the side panel is, and what it is given - the whole contract for adding one.
 *
 * The panel works out everything itself (see `usePanelModel`): the calls, the undo timeline, each
 * SQL Lab tab's own state, and each of the four workflows drawn and ready to go on screen. A layout
 * only arranges them - as tabs, as one column, side by side, whatever is being tried - so it can't
 * break any of that, and the same work can be looked at in every layout by switching between them.
 *
 * To add one: a folder beside `tabs/` and `flow/` whose `index.tsx` default-exports
 * `defineLayout({...})`, and its line in `layouts/index.ts`. Users don't pick layouts directly: a
 * theme names the layout it uses (see `THEMES` in `panelTheme.ts`), so a new layout is put on screen
 * by a theme that names it. Its id goes out with every piece of feedback (see `feedback.ts`), so it
 * can be compared with the others in the telemetry.
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
  /** Not offered at all - Ask, when the plugin runs without database information, since SQL written
   * from words could only guess at table names. A layout leaves it out, tab and all. */
  hidden: boolean;
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

/**
 * Asking by prompting (Alpha's Ask tab): a question writes SQL, and each follow-up changes the SQL
 * written so far. Only the latest answer is shown - what was asked isn't kept.
 */
export interface ConversationView {
  /** A question: SQL written from scratch, into the editor. */
  ask: (text: string) => void;
  /** A follow-up: the SQL in the editor changed as it says (a refinement). */
  followUp: (text: string) => void;
  /** Whether there's SQL for a follow-up to change - with none, a follow-up is asked as a question. */
  hasSql: boolean;
  canSend: boolean;
  sending: boolean;
  /** Opens Break it down under the editor and has it explain the editor's SQL (`requestBreakdown`). */
  explainQuery: () => void;
  /** Why nothing can be sent right now (another call is running), or null when something can. */
  waitingOn: string | null;
  /** The latest question's or follow-up's progress, then its result - null when neither has run.
   * A refinement it suggests is handed to `onSuggestion`, to be said as the next follow-up. */
  latest: (onSuggestion: (text: string) => void) => React.ReactNode;
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
  conversation: ConversationView;
  /** Accelerate and Fix as a response that grows downwards: the progress stays on screen once the
   * call lands, marked done, with the result under it - for a layout that scrolls the whole
   * response (Alpha's tabs). Accelerate is null when it hasn't run; Fix has an empty state of its own. */
  flowing: {
    accelerate: React.ReactNode;
    fix: React.ReactNode;
    /** Starts explaining and fixing the editor's errors, if there are some not yet looked at -
     * Alpha's Fix tab does it as soon as it's clicked. */
    runFix: () => void;
    /** True when the editor's SQL has errors: Accelerate doesn't run until they're fixed. */
    sqlErrors: boolean;
    /** Runs Accelerate (unless `sqlErrors`) and puts a faster version that passed its checks
     * straight into the editor, as a step in the undo timeline - the way Ask's SQL lands. One the
     * checks found may return different results waits for the user to apply it. */
    runAccelerate: () => void;
    /** Accelerate's state, for a layout that runs it without being asked - null with no SQL.
     * `current`: its latest result is for the SQL as it is now, so running it again would only
     * repeat it. */
    accelerateStatus: { disabled: boolean; running: boolean; current: boolean } | null;
  };
}

export interface LayoutProps {
  model: PanelModel;
}

export interface LayoutDefinition {
  /** Unique, and kept: it is what a theme names (see `panelTheme.ts`) and what feedback is filed
   * under, so renaming one splits its history in two. */
  id: string;
  /** What it is called, for the people working on the panel. */
  name: string;
  /** What is different about it, in a sentence. */
  description: string;
  /** Whether the "does the assistant know this database" strip (`PanelModel.schemaBar`) goes under
   * the header's ⋯ menu rather than in the layout - for a layout that leaves it out of its body. */
  schemaBarInMenu?: boolean;
  /** Everything under the header and the tab's database bar, the full height of the panel (a
   * column, so a fragment of rows is fine) - see `LayoutBody` for the scrolling part. */
  Component: React.ComponentType<LayoutProps>;
}

/** A layout, checked against `LayoutDefinition` - what each layout's `index.tsx` default-exports. */
export function defineLayout(definition: LayoutDefinition): LayoutDefinition {
  return definition;
}
