# UX experimentation: adding a panel layout

The side panel can be drawn in more than one layout, so different arrangements can be tried on the
same work and compared. **Tabs** (`frontend/src/layouts/tabs/`) and **Single column**
(`frontend/src/layouts/flow/`) are the first two. This page covers adding another.

## How it fits together

- `usePanelModel` (`panelModel.tsx`) does everything the panel does: the calls, the undo
  timeline, and each SQL Lab tab's own state. It hands back a `PanelModel`, with each of the four
  workflows (Ask, Refine, Fix, Describe) **already drawn**.
- A layout gets that model and only **arranges** it. It can't break the workflows, and the same
  work can be looked at in every layout.
- `frontend/src/layouts/layout.ts` is the whole contract. Read it first.

What a layout is given (`model`):

| Field | What it is |
|---|---|
| `workflows.ask` / `.refine` / `.fix` / `.describe` | Each workflow: `label`, `hint`, `body` (draw it as it is), `available`, `wantsAttention`, `running`, `unread`, `count`, `usesSchema` |
| `focused`, `focus(id)` | The workflow on show. The panel moves it too (undo, "Edit in Ask"), so follow it rather than keeping your own |
| `error` | The last call's failure, or null - put it where it's always seen |
| `schemaBar` | "Does the assistant know this database" - beside workflows with `usesSchema` |
| `footer(withComposer)` | The safety notes, plus the refine box when `withComposer` and there's SQL |
| `tabKey` | The SQL Lab tab's id - key any state of your own by it |

## Adding one

1. **Make a folder** under `frontend/src/layouts/`, say `stacked/`, with an `index.tsx`:

   ```tsx
   import React from 'react';
   import { Typography } from 'antd';
   import type { WorkflowSegment } from '../../tabState';
   import { defineLayout, LayoutProps } from '../layout';
   import { LayoutBody } from '../LayoutBody';

   const ORDER: WorkflowSegment[] = ['ask', 'fix', 'refine', 'describe'];

   function StackedLayout({ model }: LayoutProps) {
     const shown = ORDER.map(id => model.workflows[id]).filter(workflow => workflow.available);
     return (
       <>
         <LayoutBody model={model}>
           {model.schemaBar}
           {shown.map(workflow => (
             <section key={workflow.id}>
               <Typography.Text strong>{workflow.label}</Typography.Text>
               {workflow.body}
             </section>
           ))}
         </LayoutBody>
         {model.footer(true)}
       </>
     );
   }

   export default defineLayout({
     id: 'stacked',
     name: 'Stacked',
     description: 'Every workflow with something to do, one under another, all open.',
     Component: StackedLayout,
   });
   ```

   The component fills everything under the header and the database bar. `LayoutBody` is the part
   that scrolls, with the error at its top. Put anything that shouldn't scroll (like the tabs strip)
   above it, and the footer below it.

2. **Register it** with one line in `frontend/src/layouts/index.ts`:

   ```ts
   import stacked from './stacked';
   export const LAYOUTS: LayoutDefinition[] = withUniqueIds([tabs, flow, stacked]);
   ```

   The order is the menu order, and the first entry is the default.

That's all. From then on, the layout is automatically:
- in the header's layout menu;
- on the options page (**Ctrl+,**), with its `description` under its name;
- in the **Ctrl+Shift+L** cycle;
- remembered in the browser once chosen;
- named in every piece of feedback as `variant.layout`, so the telemetry viewer's "by variant" card
  compares it with the others.

## Rules of thumb

- **`id` is permanent.** It's stored in browsers and in feedback history, so renaming it splits that
  history. If it's later removed, browsers that had it fall back to the default.
- **Don't call APIs or keep workflow state in a layout.** If a workflow itself needs to change, that
  happens in `panelModel.tsx` or the workflow's component, and every layout gets the change.
- **Anything a layout keeps for itself** (which sections are open, say) should be keyed by
  `model.tabKey`, so it doesn't follow the user to another SQL Lab tab. See `flow/index.tsx`.
- **Take colours from `useTokens()`** (`components/primitives.tsx`) so the layout works in light
  and dark themes. Don't use the warning colour for text: it's a yellow that's hard to read.
- **Respect Brief.** For any fixed text of your own, use `useWording()` (`verbosity.ts`):
  `say(full, brief)`.

## Trying it

Run `npx tsc --noEmit -p .` in `frontend/`. Then build and install as for any change (see the README):
`superset-extensions bundle` in the repo root, copy the `.supx` into Superset's
`extensions-installed/`, and restart Superset. Pick the layout from the header's layout menu.

Variants that aren't layouts (like Full/Brief) are added to the options page as one more entry in
`OPTIONS` in `components/OptionsOverlay.tsx`. To compare one of those in the telemetry, also add it
to the `variant` sent in `feedback.ts`.
