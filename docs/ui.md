# UI

The panel lives in SQL Lab's right sidebar, beside Superset's own editor. It keeps the original
layout of this extension - header, the tab's database, four workflow tabs, the safety notes
and refine box pinned at the bottom - and **opens on Refine**.

```
┌ ✦ AI Assistant                         ⋯ ┐
│ examples                                  │   ContextBar (database only)
│ [ Ask | Refine ● | Fix 2 | Describe ]     │   WorkflowTabs (spinner / count / unread dot)
├───────────────────────────────────────────┤
│ ✓ The assistant knows examples · View …   │   SchemaKnowledgeBar (Ask and Refine)
│ ┌ ⚡ Accelerate  ☑ Run query plan first ┐ │   PerformanceActions (Refine only)
│ │  ⌥ Explain the query plan            │ │
│ └───────────────────────────────────────┘ │
│  progress … / result of the last action   │
├───────────────────────────────────────────┤
│  safety note · responsibility note        │
│ [ Refine this query — e.g. “only top 5” ↑]│   RefineComposer (Refine only)
└───────────────────────────────────────────┘
```

The header's layout menu (or **Ctrl+Shift+L**) switches to **Single column**: no tabs, the same
four workflows as collapsible sections down one column - Ask, then Fix while something is reported
against the SQL, then Refine and Describe once there is SQL - opening by themselves as the SQL's
state calls for them. The choice is remembered in the browser.

Layouts are pluggable. The panel works out the workflows itself (`usePanelModel` in
`panelModel.tsx`) and hands them, already drawn, to the chosen layout, which only decides where they
go and which are on screen. A new layout is a folder under `frontend/src/layouts/` whose `index.tsx`
default-exports `defineLayout({ id, name, description, Component })`, plus its line in
`layouts/index.ts`. It then appears in the layout menu, on the options page and in the Ctrl+Shift+L
cycle. Its id is what the browser remembers and what feedback is filed under. What a layout is
given is all in `layouts/layout.ts`.

Beside the layout menu, a small **Brief** checkbox asks for short descriptions and explanations
everywhere the panel shows them (and the breakdown leaves out its detailed explanations). It's a
request to the model, not a trim of what came back, so it applies to what's written next; anything
already on screen stays as it was written. Brief also cuts the panel's own help text (the notes,
empty states, hints and tooltips, here and in the panes under the editor) to a line where it can,
and that changes at once. Remembered in the browser.

While the panel is being developed, **Ctrl+,** (or ⋯ → Options…) lays an options page over it for
comparing variants: the layout and Full/Brief for now, each choice shown with what it does, applied
at once and remembered in the browser, with Reset to defaults. Esc or Ctrl+, closes it. A new
variant is one more entry in `OPTIONS` in `OptionsOverlay.tsx`.

Warnings and errors look the same wherever they appear, so they stand out from the panel's grey
boxes without shouting: a pale amber (or red) tint, a thin bar of the full colour down the left
edge, and a filled warning (or error) mark. That covers the "hasn't been told about the tables"
bar, a question waiting for an answer, Accelerate's findings when the rewrite may differ, and "No
database selected". The Fix card gets the red border, bar and heading but no red wash, since it
holds a long explanation to read. Warnings inside other things, such as a breakdown section's index
hints or a plan's flagged steps, are one line each with the same mark.

Under every result the model wrote - generated SQL, a failed attempt, a fix, a faster version, a
plan explanation, a description, a breakdown - a small "Was this helpful? 👍 👎" sits at the end of
the line in the quietest text colour. Either thumb is one click and is sent at once. A thumbs down
then offers the three or four likeliest reasons for that kind of result ("Wrong tables or
columns", "Not faster", "Too vague"…; "Too long" under Full, "Too short" under Brief), each one more
click, and "Other…" for a short note - the only time anything needs typing. "Change" reopens them.
It all goes into the trace of the request that produced the result (see
[architecture.md](architecture.md#telemetry-telemetrypy-otlppy)).

Terms in the model's prose that a SQL user may not know - "O(n log n)", "hash join", "TABLE ACCESS
FULL" - are drawn slightly stronger, on a faint highlight with a dotted underline; hover one (or tab
to it) for what it means here. The model picks them, a few at most per answer, and marks them as it
writes (`NLSQL_MARK_TERMS` turns it off). Never in SQL, and never in a description or suggestion
that goes back into the question box.

Under the editor, beside Results and Query history, are two more tabs: **Break it down** and
**Query Estimator** (below).

## Interrogate DB

At the top of Ask and Refine, a strip says whether the assistant knows the tab's database. If it
doesn't: "The assistant hasn't been told about the tables in `examples`" and **Interrogate DB**.
While it runs, the usual progress panel (list the schemas → read the tables, columns and indexes,
schema by schema → estimate table sizes, plan by plan → save) with its own Stop. Once done: "The
assistant knows `examples`", how many tables and views in how many schemas, indexes, how many sized,
when and by whom, and **View** (everything it was told - by schema, with row estimates, columns,
indexes, joins and any problems such as tables the database refused to plan; filterable, the first
150 shown), **Interrogate again** and **Forget**. What's known is per database, not per SQL Lab tab,
schema or user: every tab on the database shows the same, and an interrogation started in any shows
in all. The tab's selected schema plays no part - here, as everywhere in the extension.

Describing the tables with the model (`NLSQL_INTERROGATE_DESCRIPTIONS`) is off by default; with it on
the progress panel gains a "describe the tables" step and View shows the descriptions.

## Why Refine first

The first deployments have no semantic description of their databases. Without one, turning a
question into SQL is the weakest thing the panel does - the model doesn't know the tables - while
describing a query, refining it, fixing it and making it faster need nothing but the query (and the
database's plan of it). `tabState.DEFAULT_SEGMENT` is the one place this is decided.

## The tabs

| Tab | Starts from | Does | Result shown as |
|---|---|---|---|
| **Ask** | a question | question → SQL (grounded "Update SQL" once there is a question/SQL pair) | `SuccessResult` / `ClarificationsCard` / `FailureResult` + undo/redo |
| **Refine** | SQL in the editor | instruction → edited SQL; **Accelerate**; **Explain the query plan** | the latest of: refine result, `AccelerateCard`, `PlanExplanationCard` |
| **Fix** | errors on the SQL (validator gutter markers or a failed run) | explain + correct | `FixErrorsCard` |
| **Describe** | SQL in the editor | one-paragraph description ("Open in Ask" grounds it); **Break it down** | text + `SqlBreakdown` |

A hand edit of the SQL is a step in the undo timeline; its note shows on Refine.

### Accelerate

"Accelerate" asks for a faster query that returns the same result. **Run query plan first** (on by
default, per tab, disabled when the tab has no database) has the database plan the query first -
it's never run - so the model sees where the time goes. The progress panel streams the rewrite and
then why it should be faster. The `AccelerateCard` then shows:

- a verdict tag: *No difference found* / *May return different results* / *Not valid* / *No faster
  version* (see `docs/architecture.md` for what each means and doesn't);
- the planner's estimated cost before → after;
- the explanation, the list of changes, and what the checks found;
- what it was based on (the plan, or the SQL alone and why);
- collapsible: the diff, the plan the model saw, and the rewrite's plan;
- **Apply in this tab** (an undo step: "Undo puts the original query back") or **Open in a new tab**
  (a new SQL Lab tab on the same database/schema, titled "Faster: …"), and Copy.

### Break it down (the description "visual")

A first step towards describing a query *in* the editor. The `SqlBreakdown` view shows:

- an overview (what the query is for);
- the SQL itself, read-only, with line numbers and a coloured bar down the gutter for each section;
- a card per section - title, kind (CTE, Columns, Source, Join, Filter, Grouping, …), line range,
  and what that part does and why.

Hovering a card tints its lines; hovering a line highlights the card for the narrowest section that
covers it. Clicking a card (or Enter on it) **selects those lines in the real SQL editor** and
scrolls to them - possible already through the extension API's `setSelection`/`scrollToLine`. If the
editor's SQL has changed since, the view says so (the out-of-date chip, below) and clicking is
disabled (the line numbers would be wrong).

The same breakdown also has a tab of its own under the editor - **Break it down** - laid out like
the "AI Assistant SQL Query Overview" design: **Query intent** across the top (the overview); the SQL
on the left, laid out one clause per line ("Lay the SQL out first", on by default) and
syntax-coloured, each section a tinted, rounded block with its number in a circle beside its first
line; on the right **Show detailed explanation** (a switch) over a card per section - its number,
its title ("recent_users (CTE)", "Final query"), a "Lines 2–9" chip and a chevron that opens its
detailed explanation (the switch opens them all). A query with CTEs is split one section per CTE plus
the final query. Hover either side to light up the other (each scrolls the other into view); click
to keep a section lit. Clicking selects the lines in the editor too, when the SQL shown
is the editor's own (not laid out, and unchanged since) - and then moving the cursor in the editor
lights up the section it's in and scrolls its card into view (read every 400 ms while the tab is on
screen: the extension API reports content changes but not cursor moves). Started from either place,
the breakdown shows in both.

Each card also lists the **tables its part reads** - `crime_data.crimes ≈8.6M`, from Interrogate DB,
not the model - and an amber **hint** under a join or filter on a big table (1M rows or more) on a
column no index starts with: "Filters crime_data.crimes (≈8.6M rows) on district, which no index
starts with - this may read the whole table." Nothing when the database hasn't been interrogated, or
has no indexes at all (Trino/Iceberg).

- **Streaming.** The cards arrive one at a time as the model finishes each, with a "Writing section
  N…" card below the last, and the SQL's blocks fill in with them.
- **Out of date** (the editor's SQL has changed since): an amber chip, "Out of date · 2 lines
  changed", by the button; the section colours stay, and an amber bar in the margin marks each line
  that has changed in the editor since (only where the SQL shown is the editor's own - a laid-out
  copy has nothing to line it up against).
- **Being redone** ("Break it down again"): a blue "Updating · 6 s" chip; the old blocks go grey and
  hatched, with a bar sweeping along the top of the code; the old intent and cards stay readable but
  dimmed under the progress until the new sections start to arrive, which then fade in.
- **Remembered.** A tab keeps its last 12 breakdowns by the SQL they were made from, so undoing back
  to SQL broken down before shows that breakdown again, current, with no call.
- **⋯ menu**: "Lay the SQL out first" (on by default), Copy SQL, **Copy as Markdown** (intent, each
  section with its lines, explanation, tables and hints, then the SQL - for a review or a ticket), and
  **Use this layout in the editor** - also a button when the breakdown is of a laid-out copy: it puts
  that layout in the editor as an undoable step, after which the sections can be selected there.

The same chip - out of date, or updating - is used for every result kept beside the SQL it was made
from: the side panel's description and breakdown, a faster version and a plan's explanation. Asked
for again, Accelerate and Explain the query plan leave their previous result in place, dimmed, under
the progress until the new one lands.

Next steps when there's time: highlight in the editor on hover rather than click (needs decorations,
which the extension API doesn't expose - only selections and annotations), and a "map" view of CTEs
and joins.

## Query Estimator

The tab under the editor modelled on Superset's example estimator extension: **Re-estimate** at the
top right; then **Resource impact** (a bar and Low / Moderate / High / Critical), **Metrics** (rows,
cost, time - Oracle's own estimate, N/A elsewhere since nothing is run - and plan steps), **Warnings**
(each with a title, what was found, and what to do: cartesian product, very high cost, large full
scan, a table rescanned in a loop, a large sort) and the **Query plan** as a collapsible tree -
operation, the table in the link colour, rows and cost, and "details" for its conditions. It plans the
query when the tab comes into view with SQL it hasn't seen; after that, a changed query says so and
waits for Re-estimate. Its three columns sit side by side when the pane is wide enough (as in the
design) and wrap below one another when it isn't.

## Compare with original

Every change the panel makes to SQL that's already in the editor remembers what it was: a
refinement, an update from Ask, an applied fix, or an acceleration (applied in the tab, or opened in
a new one). SQL written from scratch isn't remembered, since it changed nothing. While the tab's SQL
descends from such a change, a **Compare with original** tab appears under the editor beside the
others: the original (or any later version, from a menu) against the SQL
in the editor now, side by side or inline, with unchanged stretches folded away, a +/− line count,
and Copy / Open in a new tab / Forget the original. The versions are labelled by what made them
(Refined, Updated, Fixed, Accelerated, or Edited by hand). A change made after undoing back to an
earlier version carries on from that version, and the versions after it are dropped. Undo back to
the original and the tab goes; it survives a reload, and is forgotten when the SQL Lab tab closes.
A brand-new SQL Lab tab gets a temporary id from Superset until the page reloads, and a permanent
one after. The history is stored by tab id, so after that first reload it's found again by the
tab's SQL, which matches a version in it (`adoptAncestry` in `ancestry.ts`).

**Notes** (a switch in its toolbar, on by default, remembered for the session) shows why each change
was made. Each change made since the version being compared gets a note: what was changed and why,
in a box under the last changed line its SQL covers (models often quote an unchanged line of context
too). The SQL the change is about is underlined, on either side. Rows with a note stay visible when unchanged lines are folded. A change that has since
been undone or rewritten again, so its SQL is on neither side, gets no note. A change that quoted no
SQL is listed above the diff. The same changes, with their reasons, are listed as **What changed** in
the Refine and Ask results and as **Changes** on the Accelerate card.

## Calls and SQL Lab tabs

The panel is one long-lived instance for the whole of SQL Lab; everything is kept per SQL Lab tab
(`tabState.ts`), and a call always writes back to the tab that started it - captured before its first
`await`. The SQL itself is never written straight into an editor after an `await`: it goes into that
tab's `pendingSql`, and an effect writes it into the editor when (and only when) that tab is active
and its own editor has resolved. That fixes the case where the user switches tabs while a call is
running and the SQL used to land in the wrong tab, or nowhere (ported from sql-helper, together with
the render-time editor reset that stops the previous tab's editor being paired with the new tab's
state for one render).

Each call kind has a home tab (`segmentForCallKind`); the tab bar shows a spinner on a tab with a
call running, a red count on Fix, and a dot on a tab whose result landed while the user was
elsewhere. There are three call slots per SQL Lab tab: one for SQL-producing calls (generate,
update, refine), one for describe/breakdown, and one for the performance calls (accelerate, explain
plan), plus the fix workflow's own - each progress panel has its own Stop, which cancels both the
fetch and the backend's connection to the model.
