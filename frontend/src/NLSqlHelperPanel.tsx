import React, { useLayoutEffect, useRef } from 'react';
import { Flex, Typography } from 'antd';
import { ContextBar } from './components/ContextBar';
import { PanelHeader } from './components/PanelHeader';
import { useTokens } from './components/primitives';
import { useLayout } from './panelLayout';
import { usePanelModel } from './panelModel';

/**
 * SQL Lab's own button for collapsing the sidebar (the antd splitter's collapse handle) sits just
 * right of the divider - inside the panel, over whatever is at its left edge. This puts it just
 * left of the divider instead, outside the panel. Only the divider straight before this panel's
 * sidebar is touched (`:has`), not SQL Lab's others.
 */
const MOVE_COLLAPSE_HANDLE = `
.ant-splitter-bar:has(+ .ant-splitter-panel [data-ai-assistant-panel]) .ant-splitter-bar-collapse-bar-end {
  left: auto !important;
  right: 3px !important;
}`;

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
  const { Component: Layout, schemaBarInMenu } = useLayout();
  const token = useTokens();
  const root = useRef<HTMLDivElement>(null);
  // SQL Lab wraps the panel in a div of no set height, so "100%" here would shrink to fit the
  // content and leave the footer floating halfway up the sidebar. Stretch that wrapper to the
  // sidebar it sits in instead.
  useLayoutEffect(() => {
    const wrapper = root.current?.parentElement;
    if (wrapper && !wrapper.style.height) wrapper.style.height = '100%';
  }, []);

  return (
    <Flex ref={root} vertical data-ai-assistant-panel="" style={{ height: '100%', background: token.colorBgContainer, position: 'relative' }}>
      <style>{MOVE_COLLAPSE_HANDLE}</style>
      <PanelHeader schemaBar={schemaBarInMenu && model ? model.schemaBar : undefined} />
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
