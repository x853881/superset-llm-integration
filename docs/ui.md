# UI

The extension adds a panel to SQL Lab's right sidebar and tabs under the editor. It never replaces
Superset's own editor: SQL it writes goes into the editor as a step you can undo, and nothing is
ever run for you.

The panel is only shown to users whose role has `can_read` on `NlToSqlAndBackAPI`.

## The side panel

```
┌ ✦ AI Assistant                         ⋯ ┐   PanelHeader
│ Active database: examples                 │   ContextBar
│ [ Ask | Accelerate | Fix 2 ]              │   WorkflowTabs
├───────────────────────────────────────────┤
│  progress, then the result                │
├───────────────────────────────────────────┤
│  safety note · responsibility note        │
│ [ Ask a question…                    Ask ]│   AskComposer (Ask only)
└───────────────────────────────────────────┘
```

The header's ⋯ menu has **Database knowledge…** and **Change theme**.

The tabs:

- **Ask** - type a question and the SQL is written into the editor. Each prompt after that changes
  the SQL written so far. If the model needs more to go on, it asks clarifying questions with
  clickable options; with several, the answers are sent once each has one picked. **New question**
  starts again; **Explain query** opens Break it down under the editor.
- **Accelerate** - clicking the tab asks for a faster query that returns the same result. A faster
  version that passes its checks goes straight into the editor, shown as a diff - unless the
  checks found it may return different results: then it shows what they found and waits for
  **Apply anyway**. It doesn't run while the SQL has errors.
- **Fix** - clicking the tab fixes the errors on show (from Superset's validator or a failed run).
  The corrected SQL goes into the editor, shown as a diff.

Each tab shows a call's progress (its steps, the model's thinking, and Stop), which stays on screen
once it lands with the result under it: "Query updated." with Undo, Redo and Copy. The tab bar shows
a spinner while a call runs, the number of errors on Fix, and a dot where a result landed while you
were elsewhere.

Everything is kept per SQL Lab tab (`tabState.ts`), and a call always writes back to the tab that
started it, even if you switch tabs while it runs.

### Themes

**Change theme** sets the panel's layout and how much the model and the panel write (Full or
Brief). **Alpha** is the default: Tabs, with full text. The choice is remembered in the browser.
Themes are listed in `panelTheme.ts`.

### Interrogate DB

Reads the structure of the tab's database - schemas, tables, columns, keys, indexes and the
planner's row estimates, never the data - so the model knows the tables when writing SQL. It's kept
per database and shared by everyone using it. **View** shows what was found; **Read again**
and **Forget** are beside it.

When the plugin runs without database information (`NLSQL_DB_INFO = False`) none of this shows,
and neither does Ask: with no tables to go on, SQL written from words could only guess at their
names. Every layout leaves it out; the rest of the panel is as it was.

## Under the editor

Beside Results and Query history:

- **Break it down** - opening the tab explains the editor's SQL part by part: the query's intent,
  then the SQL with each section marked and a card per section. Hovering or clicking a card
  highlights its lines (and selects them in the editor). Cards list the tables each part reads and
  warn about filters or joins on big unindexed columns. It's marked out of date when the SQL changes.
- **Query Estimator** - the planner's estimate of the query (it isn't run): resource impact, rows,
  cost, warnings and the query plan.
- **Compare with original** - appears once the panel has changed SQL that was already in the
  editor: the original against the SQL now, with a note under each change saying why it was made.

## Everywhere

- **Feedback** - a small 👍 👎 under each result the model wrote; a thumbs down offers a few likely
  reasons. It's recorded against the request's trace (see
  [architecture.md](architecture.md#telemetry-telemetrypy-otlppy)).
- **Terms** - technical terms in the model's text ("hash join", "O(n log n)") are highlighted; hover
  for what they mean.
