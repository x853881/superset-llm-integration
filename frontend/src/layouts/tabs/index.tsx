import React, { useLayoutEffect } from 'react';
import { Typography } from 'antd';
import { components } from '@apache-superset/core';
import { DEFAULT_SEGMENT } from '../../tabState';
import type { WorkflowSegment } from '../../tabState';
import { EmptyWorkflow } from '../../components/EmptyWorkflow';
import { defineLayout, LayoutProps } from '../layout';
import type { PanelModel, WorkflowView } from '../layout';
import { useTokens } from '../../components/primitives';
import { FlowingResponse } from './FlowingResponse';
import { WorkflowTabs } from './WorkflowTabs';
import { Conversation } from './Conversation';

/** The workflows with a tab. Describe has none: describing a query is left to Break it down, the tab
 * under the editor. */
const SEGMENTS: WorkflowSegment[] = ['ask', 'refine', 'fix'];

/** The SQL Lab tabs Alpha has been shown for. Alpha opens each tab on Ask, not on the panel's
 * default (`DEFAULT_SEGMENT`, which the other layouts keep) - but only the first time, so a tab the
 * user moved stays where they left it. */
const OPENED = new Set<string>();

/**
 * Ask, Accelerate and Fix as tabs, one on screen at a time - the panel's first layout and its
 * default. Ask is a conversation (`Conversation`). What the assistant knows about the database is
 * under the header's ⋯ menu (`schemaBarInMenu`), not here.
 *
 * Refine's tab is Accelerate, and nothing else: clicking it runs Accelerate there and then (see
 * `select`), and it shows that call's progress and result - no refine box, no Explain the query plan.
 */
function TabsLayout({ model }: LayoutProps) {
  const token = useTokens();
  const { workflows } = model;
  // Should anything still move the panel to Describe, the Accelerate tab is shown instead of a
  // workflow with no tab.
  const segments = SEGMENTS.filter(segment => !workflows[segment].hidden);
  const opening = !OPENED.has(model.tabKey) && model.focused === DEFAULT_SEGMENT && segments.includes('ask');
  useLayoutEffect(() => {
    if (OPENED.has(model.tabKey)) return;
    OPENED.add(model.tabKey);
    if (model.focused === DEFAULT_SEGMENT && !workflows.ask.hidden) model.focus('ask');
    // Only on a tab's first showing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model.tabKey]);
  const focused = opening ? 'ask' : segments.includes(model.focused) ? model.focused : 'refine';
  const accelerate = model.flowing.accelerateStatus;
  const tabs: Record<WorkflowSegment, WorkflowView> = {
    ...workflows,
    // A follow-up is a refinement, whose home is Refine's tab - but here it belongs to Ask's
    // conversation, so only Accelerate's own call shows on the Accelerate tab.
    ask: { ...workflows.ask, running: workflows.ask.running || model.conversation.sending },
    refine: {
      ...workflows.refine,
      label: 'Accelerate',
      hint: 'A faster query that returns the same result - runs as soon as you click',
      running: Boolean(accelerate?.running),
      // Accelerate only runs when its tab is clicked, so a result arriving elsewhere is a follow-up's.
      unread: false,
    },
  };

  /** Opening the Accelerate tab is asking for a faster version - unless one for this SQL is on show
   * already, or it can't run now; opening the Fix tab is asking for the errors to be fixed. */
  const select = (segment: WorkflowSegment) => {
    model.focus(segment);
    if (segment === 'refine' && accelerate && !accelerate.disabled && !accelerate.running && !accelerate.current) {
      model.flowing.runAccelerate();
    }
    // So is opening the Fix tab with errors on show: it starts fixing them there and then.
    if (segment === 'fix') model.flowing.runFix();
  };

  return (
    <>
      <WorkflowTabs value={focused} onChange={select} workflows={tabs} segments={segments} />
      {focused === 'ask' ? (
        // Ask is a conversation, which scrolls itself and keeps its box at the foot.
        <>
          {model.error && <div style={{ padding: `0 ${token.paddingSM}px` }}>{model.error}</div>}
          <Conversation tabKey={model.tabKey} conversation={model.conversation} />
        </>
      ) : (
        // Accelerate and Fix read the same way as Ask: the progress kept once the call lands, the
        // result under it, and the one scrollbar (`FlowingResponse`).
        <FlowingResponse running={tabs[focused].running} style={{ padding: `0 ${token.paddingSM}px ${token.paddingSM}px` }}>
          {model.error}
          {focused === 'refine'
            ? model.flowing.sqlErrors
              ? <FixFirst onFix={() => select('fix')} />
              : model.flowing.accelerate ?? <AccelerateEmpty action={accelerate} run={model.flowing.runAccelerate} askHidden={workflows.ask.hidden} />
            : model.flowing.fix}
        </FlowingResponse>
      )}
      {model.footer(false)}
    </>
  );
}

/** The Accelerate tab while the editor's SQL has errors: speeding up a query that doesn't work would
 * mean the model fixing it along the way, so it doesn't run - the errors are the Fix tab's job. */
function FixFirst({ onFix }: { onFix: () => void }) {
  const token = useTokens();
  return (
    <div style={{ paddingTop: token.paddingSM }}>
      <components.Alert type="warning" showIcon closable={false}>
        <Typography.Text strong>SQL error - fix it first.</Typography.Text> Accelerate runs once the query in the editor has no
        errors. <Typography.Link onClick={onFix}>Go to Fix</Typography.Link>
      </components.Alert>
    </div>
  );
}

/** The Accelerate tab with nothing run yet - say, the panel reopened on it, which shouldn't call the
 * model by itself. */
function AccelerateEmpty({
  action,
  run,
  askHidden,
}: {
  action: PanelModel['flowing']['accelerateStatus'];
  run: () => void;
  askHidden: boolean;
}) {
  if (!action) {
    return (
      <EmptyWorkflow
        next={`Type or paste some SQL in the editor${askHidden ? '' : ' (or ask for some on the Ask tab)'}, then click Accelerate.`}
      >
        There is no SQL in the editor to speed up yet.
      </EmptyWorkflow>
    );
  }
  return (
    <EmptyWorkflow next="Clicking the Accelerate tab asks for a faster version of the query in the editor.">
      {action.disabled ? (
        'Accelerate can run once the call in progress has finished.'
      ) : (
        <Typography.Link onClick={run}>Accelerate this query</Typography.Link>
      )}
    </EmptyWorkflow>
  );
}

export default defineLayout({
  id: 'tabs',
  name: 'Tabs',
  description: 'Ask, Accelerate and Fix as tabs, one on screen at a time; the Accelerate tab runs as soon as it is clicked.',
  schemaBarInMenu: true,
  Component: TabsLayout,
});
