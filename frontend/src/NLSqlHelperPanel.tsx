import React from 'react';
import { Flex, Typography } from 'antd';
import { ContextBar } from './components/ContextBar';
import { OptionsOverlay } from './components/OptionsOverlay';
import { PanelHeader } from './components/PanelHeader';
import { useTokens } from './components/primitives';
import { useLayout, useLayoutShortcut } from './panelLayout';
import { usePanelModel } from './panelModel';

/**
 * A `sqllab.rightSidebar` panel that sits alongside Superset's own SQL editor (Ace or Monaco,
 * whichever is active) rather than replacing it - unlike an earlier version of this extension,
 * which registered a whole replacement editor.
 *
 * Four workflows: Ask (a question -> SQL), Refine (change the SQL by instruction, or make it
 * faster - Accelerate - or have its plan explained), Fix (explain and correct the errors reported
 * against it) and Describe (SQL -> what it does, as one sentence or section by section). It opens on Refine: see `DEFAULT_SEGMENT`. Generated SQL is written into the
 * real editor and left there - this panel never runs anything (planning a query is the most it ever
 * asks of the database), which is what the standing note above the refine box says and why every
 * result is presented as something to review rather than an answer.
 *
 * "Generate/Update SQL" and "Refine" don't just hand back SQL text - the backend self-reports
 * whether it could produce it at all (see `SqlGenerationResult`), and whichever of the three
 * shapes it reports is rendered: the review detail on success, the clarifying questions it needs
 * answered before it will write anything (`ClarificationsCard`), or what it looked at and what to
 * try instead when it can't (`FailureResult`). Every attempt - whatever it produced - and any hand
 * edit of the SQL are recorded as a step in the undo/redo timeline behind the result actions; a
 * step that didn't change the SQL still occupies a place in it so its detail isn't lost.
 *
 * This panel is a single instance for the whole SQL Lab session (see `index.tsx`), not one per
 * tab, so it tracks whichever tab is currently active itself (`useActiveSqlLabTab`) and keeps
 * each tab's own question text, undo/redo history and last result separate (`useTabState`,
 * keyed by that tab's own id) rather than sharing one set of state across every open tab. A call
 * that finishes after the user has switched tabs writes its SQL into its own tab's `pendingSql`,
 * which lands in that tab's editor when the user comes back to it - never into whichever editor
 * happens to be on screen.
 *
 * All of that is worked out by `usePanelModel`. What is drawn here is only the panel's own chrome -
 * the header and the tab's database - and below it the chosen layout (see `layouts/`), which
 * arranges the workflows as tabs, one column, or whatever else is being tried.
 */
const NLSqlHelperPanel: React.FC = () => {
  const { chrome, model } = usePanelModel();
  const { Component: Layout } = useLayout();
  useLayoutShortcut();
  const token = useTokens();

  return (
    <Flex vertical style={{ height: '100%', background: token.colorBgContainer, position: 'relative' }}>
      <OptionsOverlay />
      <PanelHeader startOverDisabled={chrome.startOverDisabled} onStartOver={chrome.onStartOver} />
      <ContextBar {...chrome.context} />
      {model ? (
        <Layout model={model} />
      ) : (
        <div style={{ flex: 1, minHeight: 0, padding: token.paddingSM }}>
          <Typography.Text type="secondary">Open a SQL Lab tab to get started.</Typography.Text>
        </div>
      )}
    </Flex>
  );
};

export default NLSqlHelperPanel;
