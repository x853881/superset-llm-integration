import React from 'react';
import { defineLayout, LayoutProps } from '../layout';
import { LayoutBody } from '../LayoutBody';
import { WorkflowTabs } from './WorkflowTabs';

/** The four workflows as tabs, one on screen at a time - the panel's first layout and its
 * default. The refine box is only at the foot of Refine, where what it changes is on show. */
function TabsLayout({ model }: LayoutProps) {
  const { workflows, focused } = model;
  const shown = workflows[focused];
  return (
    <>
      <WorkflowTabs value={focused} onChange={model.focus} workflows={workflows} />
      <LayoutBody model={model} flushTop>
        {shown.usesSchema && model.schemaBar}
        {shown.body}
      </LayoutBody>
      {model.footer(focused === 'refine')}
    </>
  );
}

export default defineLayout({
  id: 'tabs',
  name: 'Tabs',
  description: 'Ask, Refine, Fix and Describe as tabs, one on screen at a time.',
  Component: TabsLayout,
});
