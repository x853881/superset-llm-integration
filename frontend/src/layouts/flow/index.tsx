import React from 'react';
import type { WorkflowSegment } from '../../tabState';
import { defineLayout, LayoutProps } from '../layout';
import { LayoutBody } from '../LayoutBody';
import { FlowLayout } from './FlowLayout';

/** Top to bottom: what to do first, then what's wrong with it, then what to do with it. */
const ORDER: WorkflowSegment[] = ['ask', 'fix', 'refine', 'describe'];

/**
 * No tabs: one column of sections whose shape follows the SQL - Ask at the top, Fix only while
 * something is reported against the query, Refine and Describe once there is SQL to act on (after
 * sql-helper's single linear flow). The refine box stays at the foot whenever there is SQL.
 */
function SingleColumnLayout({ model }: LayoutProps) {
  const sections = ORDER.map(id => model.workflows[id]).filter(workflow => workflow.available);
  return (
    <>
      <LayoutBody model={model}>
        {model.schemaBar}
        <FlowLayout key={model.tabKey} sections={sections} focused={model.focused} onFocus={model.focus} />
      </LayoutBody>
      {model.footer(true)}
    </>
  );
}

export default defineLayout({
  id: 'flow',
  name: 'Single column',
  description: 'No tabs: the four as sections down one column, opening as the SQL calls for them.',
  Component: SingleColumnLayout,
});
