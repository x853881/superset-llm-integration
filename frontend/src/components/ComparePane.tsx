import React, { useEffect, useMemo, useState } from 'react';
import { Button, Dropdown, Flex, Segmented, Select, Switch, Tag, Tooltip, Typography } from 'antd';
import type { MenuProps } from 'antd';
import DiffOutlined from '@ant-design/icons/DiffOutlined';
import MoreOutlined from '@ant-design/icons/MoreOutlined';
import { sqlLab } from '@apache-superset/core';
import { forgetAncestry, SqlVersion, useAncestry } from '../ancestry';
import { CharRange, ComparisonNotes, lineOf, notesFor, PlacedNote, spansByLine } from '../changeNotes';
import { useFillHeight, useVisible } from '../estimate';
import { DiffLine, diffSource, lineDiff, sideBySide } from '../lineDiff';
import { highlightSql, SqlToken } from '../sqlHighlight';
import { useActiveSqlLabTab, useTabEditor } from '../sqlLabHooks';
import { isSqlEmpty } from '../tabState';
import { useWording } from '../verbosity';
import type { RewriteChange } from '../performanceResult';
import { tokenStyles } from './SqlCode';
import { useTokens } from './primitives';
import { Prose } from './Prose';

/** Unchanged lines kept either side of a change when the rest are folded away. */
const CONTEXT = 3;

/** Side by side or inline, and whether unchanged stretches are folded - one choice for the session. */
let modePreference: 'split' | 'inline' = 'split';
let foldPreference = true;
/** Whether each change's note is shown under the lines it's about (see `changeNotes`). */
let notesPreference = true;

const NO_NOTES: ComparisonNotes = { placed: [], unplaced: [] };

/** A row as drawn: a line (or a pair of them), or a fold standing in for unchanged lines. */
type Row =
  | { kind: 'same' | 'changed' | 'removed' | 'added'; oldIndex: number | null; newIndex: number | null }
  | { kind: 'fold'; start: number; count: number };

function rowsFor(diff: DiffLine[], mode: 'split' | 'inline'): Row[] {
  if (mode === 'split') return sideBySide(diff);
  return diff.map(line => ({ kind: line.kind, oldIndex: line.oldIndex ?? null, newIndex: line.newIndex ?? null }));
}

/** Folds each run of unchanged rows longer than the context either side of it into one row - except
 * rows in `keep` (those with a note under them), which stay as if they had changed. */
function fold(rows: Row[], expanded: Set<number>, keep: Map<Row, unknown>): Row[] {
  const plain = (row: Row) => row.kind === 'same' && !keep.has(row);
  const out: Row[] = [];
  let k = 0;
  while (k < rows.length) {
    if (!plain(rows[k])) {
      out.push(rows[k]);
      k += 1;
      continue;
    }
    let end = k;
    while (end < rows.length && plain(rows[end])) end += 1;
    const keepBefore = k === 0 ? 0 : CONTEXT;
    const keepAfter = end === rows.length ? 0 : CONTEXT;
    const hidden = end - k - keepBefore - keepAfter;
    if (hidden > 1 && !expanded.has(k)) {
      out.push(...rows.slice(k, k + keepBefore), { kind: 'fold', start: k, count: hidden }, ...rows.slice(end - keepAfter, end));
    } else {
      out.push(...rows.slice(k, end));
    }
    k = end;
  }
  return out;
}

function timeOf(at: number): string {
  return new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/**
 * The "Compare with original" tab under SQL Lab's editor - there only while the tab's SQL has an
 * ancestor, i.e. was changed by the panel: refined, updated, fixed or accelerated (see `ancestry`,
 * `compareView`). A diff of the SQL it started from (or any later version: an earlier change, or SQL
 * edited by hand between two) against the SQL in the editor now, side by side or inline, unchanged
 * stretches folded away, with a note on each change. Only shows the two; nothing is run, and the
 * editor isn't changed.
 */
export function ComparePane() {
  const token = useTokens();
  const say = useWording();
  const activeTab = useActiveSqlLabTab();
  const { sql } = useTabEditor(activeTab);
  const ancestry = useAncestry(activeTab?.id);
  const [root, setRoot] = useState<HTMLElement | null>(null);
  const visible = useVisible(root);
  const fillHeight = useFillHeight(visible ? root : null);
  const [baseIndex, setBaseIndex] = useState(0);
  const [mode, setMode] = useState(modePreference);
  const [folded, setFolded] = useState(foldPreference);
  const [showNotes, setShowNotes] = useState(notesPreference);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [copied, setCopied] = useState(false);
  const styles = useMemo(() => tokenStyles(token), [token]);

  const versions = ancestry?.versions ?? [];
  const latest = versions[versions.length - 1];
  const editedSince = latest !== undefined && !isSqlEmpty(sql) && sql.trim() !== latest.sql.trim();
  // Any version can be compared with the editor's SQL - except the last while the editor still holds
  // exactly that, which would only ever show no differences.
  const choices = editedSince ? versions : versions.slice(0, -1);
  const baseAt = Math.min(baseIndex, Math.max(0, choices.length - 1));
  const base: SqlVersion | undefined = choices[baseAt];

  useEffect(() => {
    setBaseIndex(0);
    setExpanded(new Set());
  }, [activeTab?.id]);
  useEffect(() => setExpanded(new Set()), [baseAt, mode]);
  useEffect(() => {
    if (!copied) return undefined;
    const handle = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(handle);
  }, [copied]);

  const before = base ? diffSource(base.sql) : '';
  const after = diffSource(sql);
  // Only worked out while on screen: SQL Lab renders this tab all the time.
  const diff = useMemo(() => (visible && base ? lineDiff(before, after) : null), [visible, base, before, after]);
  const oldLines = useMemo(() => highlightSql(before), [before]);
  const newLines = useMemo(() => highlightSql(after), [after]);
  const allRows = useMemo(() => (diff ? rowsFor(diff, mode) : []), [diff, mode]);

  // The changes made since `base`, found in the two sides by the SQL they quote.
  const notes = useMemo(
    () => (visible && base && ancestry ? notesFor(ancestry.versions, base, before, after) : NO_NOTES),
    [visible, base, ancestry, before, after],
  );
  const notesOn = showNotes && notes.placed.length + notes.unplaced.length > 0;
  const ranges = (side: 'before' | 'after') =>
    notes.placed.map(note => note[side]).filter((range): range is CharRange => range !== null);
  const oldSpans = useMemo(() => (notesOn ? spansByLine(before, ranges('before')) : new Map()), [notesOn, notes, before]);
  const newSpans = useMemo(() => (notesOn ? spansByLine(after, ranges('after')) : new Map()), [notesOn, notes, after]);
  // Each note goes under the last changed row its quoted SQL covers - in the newer SQL if it's
  // there, else the older. Not simply the quote's last line: a model often quotes a line of
  // unchanged context either side to make the quote findable, and the note belongs with the change.
  // A quote that covers no changed row (the change was a word within a line the diff counts as
  // unchanged - it ignores indentation) gets its last line.
  const notesAt = useMemo(() => {
    const at = new Map<Row, PlacedNote[]>();
    if (!notesOn) return at;
    notes.placed.forEach(note => {
      const side = note.after ? 'new' : 'old';
      const range = (note.after ?? note.before)!;
      const text = side === 'new' ? after : before;
      const first = lineOf(text, range.start);
      const last = lineOf(text, Math.max(range.start, range.end - 1));
      const lineIn = (row: Row) => (row.kind === 'fold' ? null : side === 'new' ? row.newIndex : row.oldIndex);
      const covered = allRows.filter(row => {
        const line = lineIn(row);
        return line !== null && line >= first && line <= last;
      });
      const changed = covered.filter(row => row.kind !== 'same');
      const row = changed.length > 0 ? changed[changed.length - 1] : covered.find(candidate => lineIn(candidate) === last);
      if (row) at.set(row, [...(at.get(row) ?? []), note]);
    });
    return at;
  }, [notesOn, notes, allRows, before, after]);
  const rows = useMemo(() => (folded ? fold(allRows, expanded, notesAt) : allRows), [allRows, folded, expanded, notesAt]);
  const added = diff?.filter(line => line.kind === 'added').length ?? 0;
  const removed = diff?.filter(line => line.kind === 'removed').length ?? 0;

  if (!activeTab || !base || !latest) {
    return (
      <div ref={setRoot} style={{ padding: `0 ${token.paddingXS}px` }}>
        <Typography.Text type="secondary">
          {say(
            'When the assistant changes a query - refines, updates, fixes or accelerates it - the SQL it started from is kept, and this tab compares the two.',
            'Refine, fix or accelerate a query to compare it with the original here.',
          )}
        </Typography.Text>
      </div>
    );
  }

  const status = isSqlEmpty(sql)
    ? 'The editor is empty.'
    : editedSince
      ? `Edited since the assistant's last change at ${timeOf(latest.at)}.`
      : `The editor holds the ${latest.label.toLowerCase()} version from ${timeOf(latest.at)}.`;

  const openInNewTab = async () => {
    try {
      await sqlLab.createTab({
        sql: base.sql,
        title: `${base.label}: ${activeTab.title}`,
        databaseId: activeTab.databaseId,
        catalog: activeTab.catalog,
        schema: activeTab.schema,
      });
    } catch {
      // The new tab just doesn't open.
    }
  };

  const menu: MenuProps = {
    items: [
      { key: 'copy', label: `Copy the ${base.label.toLowerCase()} SQL` },
      { key: 'open', label: `Open the ${base.label.toLowerCase()} SQL in a new tab` },
      { type: 'divider' },
      { key: 'forget', label: 'Forget the original (removes this tab)', danger: true },
    ],
    onClick: ({ key }) => {
      if (key === 'copy') navigator.clipboard?.writeText(base.sql).then(() => setCopied(true));
      else if (key === 'open') openInNewTab();
      else if (key === 'forget') forgetAncestry(activeTab.id);
    },
  };

  // What a note's quoted SQL is drawn with: an underline in the theme's primary colour, which reads
  // on the red and green of removed and added lines alike.
  const quoted: React.CSSProperties = {
    textDecorationLine: 'underline',
    textDecorationColor: token.colorPrimary,
    textDecorationThickness: 2,
    textUnderlineOffset: 3,
  };

  /** A line's tokens, coloured - and cut where `spans` (columns a note quotes) start and end, so
   * those stretches can be underlined. */
  const code = (tokens: SqlToken[] | undefined, spans?: [number, number][]) => {
    if (!tokens || tokens.length === 0) return ' ';
    let column = 0;
    return tokens.map((part, index) => {
      const start = column;
      const end = column + part.text.length;
      column = end;
      if (!spans || spans.length === 0) {
        return (
          // eslint-disable-next-line react/no-array-index-key
          <span key={index} style={styles[part.kind]}>
            {part.text}
          </span>
        );
      }
      const cuts = [start, end];
      spans.forEach(([from, to]) => [from, to].forEach(cut => cut > start && cut < end && cuts.push(cut)));
      cuts.sort((a, b) => a - b);
      return cuts.slice(0, -1).map((from, k) => {
        const to = cuts[k + 1];
        if (to === from) return null;
        const marked = spans.some(([s, e]) => s <= from && to <= e);
        return (
          // eslint-disable-next-line react/no-array-index-key
          <span key={`${index}-${from}`} style={marked ? { ...styles[part.kind], ...quoted } : styles[part.kind]}>
            {part.text.slice(from - start, to - start)}
          </span>
        );
      });
    });
  };

  /** A change's note: what was changed, and why - under the lines it's about, or above the diff for
   * one that quoted nothing to place it by. Wraps, and in inline mode stays in view as the code
   * scrolls sideways. */
  const note = (change: RewriteChange, key: string) => (
    <div
      key={key}
      style={{
        position: mode === 'inline' ? 'sticky' : undefined,
        left: 0,
        maxWidth: mode === 'inline' ? 640 : undefined,
        whiteSpace: 'normal',
        margin: `2px ${token.paddingXS}px 4px`,
        padding: `2px ${token.paddingSM}px`,
        borderLeft: `3px solid ${token.colorPrimary}`,
        borderRadius: token.borderRadiusSM,
        background: token.colorFillQuaternary,
        fontFamily: token.fontFamily,
        fontSize: token.fontSizeSM,
        lineHeight: token.lineHeight,
      }}
    >
      <Typography.Text strong style={{ fontSize: 'inherit' }}>
        <Prose text={change.summary} />
      </Typography.Text>
      {change.why && (
        <Typography.Text type="secondary" style={{ fontSize: 'inherit' }}>
          {change.summary ? ' - ' : ''}
          <Prose text={change.why} />
        </Typography.Text>
      )}
    </div>
  );

  const gutterWidth = `${String(Math.max(oldLines.length, newLines.length)).length + 1}ch`;
  const number = (index: number | null) => (
    <span
      style={{
        width: gutterWidth,
        flexShrink: 0,
        textAlign: 'right',
        paddingRight: token.paddingXS,
        color: token.colorTextQuaternary,
        userSelect: 'none',
      }}
    >
      {index === null ? '' : index + 1}
    </span>
  );
  const marker = (kind: 'removed' | 'added' | null) => (
    <span
      style={{
        width: '2ch',
        flexShrink: 0,
        userSelect: 'none',
        color: kind === 'removed' ? token.colorErrorText : kind === 'added' ? token.colorSuccessText : undefined,
      }}
    >
      {kind === 'removed' ? '−' : kind === 'added' ? '+' : ''}
    </span>
  );
  const tint = (kind: 'removed' | 'added' | 'same' | 'empty') =>
    kind === 'removed'
      ? token.colorErrorBg
      : kind === 'added'
        ? token.colorSuccessBg
        : kind === 'empty'
          ? token.colorFillQuaternary
          : undefined;

  /** One side of a side-by-side row: wrapped rather than scrolled, so the two sides stay level. */
  const half = (index: number | null, kind: 'removed' | 'added' | 'same', lines: SqlToken[][], left: boolean) => (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        display: 'flex',
        background: tint(index === null ? 'empty' : kind),
        borderRight: left ? `1px solid ${token.colorBorderSecondary}` : undefined,
      }}
    >
      {number(index)}
      {marker(index === null || kind === 'same' ? null : kind)}
      <span style={{ flex: 1, minWidth: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', paddingRight: token.paddingXS }}>
        {index === null ? '' : code(lines[index], (left ? oldSpans : newSpans).get(index))}
      </span>
    </div>
  );

  const body = !diff ? (
    <Typography.Text type="secondary">
      {visible ? say('Too long to compare here - more than 600 lines on one side.', 'Too long to compare (over 600 lines).') : ''}
    </Typography.Text>
  ) : added === 0 && removed === 0 ? (
    <Typography.Text type="secondary">
      No differences, apart from indentation and spacing at the ends of lines.
    </Typography.Text>
  ) : (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        overflow: 'auto',
        fontFamily: token.fontFamilyCode,
        fontSize: token.fontSizeSM,
        lineHeight: 1.6,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadiusLG,
        background: token.colorBgContainer,
      }}
    >
      {mode === 'split' && (
        <Flex
          style={{
            position: 'sticky',
            top: 0,
            zIndex: 1,
            // The fill is translucent: laid over the container's own colour so what scrolls under it
            // (a note especially) doesn't show through.
            background: `linear-gradient(${token.colorFillTertiary}, ${token.colorFillTertiary}), ${token.colorBgContainer}`,
            borderBottom: `1px solid ${token.colorBorderSecondary}`,
            fontFamily: token.fontFamily,
            fontSize: token.fontSizeSM,
            color: token.colorTextSecondary,
          }}
        >
          <div style={{ flex: 1, padding: `2px ${token.paddingSM}px`, borderRight: `1px solid ${token.colorBorderSecondary}` }}>
            {base.label} · {timeOf(base.at)}
          </div>
          <div style={{ flex: 1, padding: `2px ${token.paddingSM}px` }}>In the editor now</div>
        </Flex>
      )}
      <div style={{ display: mode === 'inline' ? 'inline-block' : 'block', minWidth: '100%' }}>
        {notesOn && notes.unplaced.map((change, index) => note(change, `unplaced-${index}`))}
        {rows.map(row => {
          if (row.kind === 'fold') {
            return (
              <button
                key={`fold-${row.start}`}
                type="button"
                onClick={() => setExpanded(previous => new Set(previous).add(row.start))}
                style={{
                  display: 'block',
                  width: '100%',
                  border: 'none',
                  borderTop: `1px solid ${token.colorBorderSecondary}`,
                  borderBottom: `1px solid ${token.colorBorderSecondary}`,
                  background: token.colorFillQuaternary,
                  color: token.colorTextTertiary,
                  fontFamily: token.fontFamily,
                  fontSize: token.fontSizeSM,
                  padding: `1px ${token.paddingSM}px`,
                  textAlign: 'left',
                  cursor: 'pointer',
                }}
              >
                ⋯ {row.count} unchanged line{row.count === 1 ? '' : 's'} - show
              </button>
            );
          }
          const key = `${row.kind}-${row.oldIndex}-${row.newIndex}`;
          const below = (notesAt.get(row) ?? []).map(placed => note(placed.change, `note-${placed.number}`));
          if (mode === 'split') {
            const same = row.kind === 'same';
            return (
              <React.Fragment key={key}>
                <Flex>
                  {half(row.oldIndex, same ? 'same' : 'removed', oldLines, true)}
                  {half(row.newIndex, same ? 'same' : 'added', newLines, false)}
                </Flex>
                {below}
              </React.Fragment>
            );
          }
          const kind = row.kind === 'removed' || row.kind === 'added' ? row.kind : 'same';
          return (
            <React.Fragment key={key}>
              <div style={{ display: 'flex', whiteSpace: 'pre', background: tint(kind), paddingRight: token.paddingSM }}>
                {number(row.oldIndex)}
                {number(row.newIndex)}
                {marker(kind === 'same' ? null : kind)}
                <span>
                  {kind === 'removed'
                    ? code(oldLines[row.oldIndex as number], oldSpans.get(row.oldIndex as number))
                    : code(newLines[row.newIndex as number], newSpans.get(row.newIndex as number))}
                </span>
              </div>
              {below}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );

  return (
    <Flex
      ref={setRoot}
      vertical
      gap={token.marginXS}
      style={{ height: fillHeight ?? 'auto', minHeight: 240, padding: `0 ${token.paddingXS}px` }}
    >
      <Flex align="center" gap={token.marginSM} wrap="wrap" style={{ flexShrink: 0 }}>
        <DiffOutlined style={{ color: token.colorPrimary, fontSize: token.fontSizeLG }} />
        <Flex align="center" gap={token.marginXS} wrap="wrap">
          <Typography.Text>Comparing</Typography.Text>
          {choices.length > 1 ? (
            <Select
              size="small"
              value={baseAt}
              onChange={setBaseIndex}
              popupMatchSelectWidth={false}
              options={choices.map((version, index) => ({
                value: index,
                label: `${version.label} · ${timeOf(version.at)}`,
              }))}
            />
          ) : (
            <Typography.Text strong>the original SQL</Typography.Text>
          )}
          <Typography.Text>with the SQL in the editor now</Typography.Text>
        </Flex>
        {diff && (added > 0 || removed > 0) && (
          <Flex gap={token.marginXXS}>
            <Tag color="success" bordered={false} style={{ marginInlineEnd: 0 }}>
              +{added}
            </Tag>
            <Tag color="error" bordered={false} style={{ marginInlineEnd: 0 }}>
              −{removed}
            </Tag>
          </Flex>
        )}
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM, flex: 1, minWidth: 120 }} ellipsis>
          {copied ? `Copied the ${base.label.toLowerCase()} SQL` : status}
        </Typography.Text>
        {notes.placed.length + notes.unplaced.length > 0 && (
          <Tooltip title={say('Why each change was made, under the lines it changed, with what it quotes underlined', 'Why each change was made')}>
            <Flex align="center" gap={token.marginXXS}>
              <Switch
                size="small"
                checked={showNotes}
                onChange={checked => {
                  notesPreference = checked;
                  setShowNotes(checked);
                }}
              />
              <Typography.Text style={{ fontSize: token.fontSizeSM }}>Notes</Typography.Text>
            </Flex>
          </Tooltip>
        )}
        <Tooltip title={say('Fold away unchanged stretches, keeping a few lines either side of each change', 'Hide unchanged lines')}>
          <Flex align="center" gap={token.marginXXS}>
            <Switch
              size="small"
              checked={folded}
              onChange={checked => {
                foldPreference = checked;
                setFolded(checked);
              }}
            />
            <Typography.Text style={{ fontSize: token.fontSizeSM }}>Only changes</Typography.Text>
          </Flex>
        </Tooltip>
        <Segmented
          size="small"
          value={mode}
          onChange={value => {
            modePreference = value as 'split' | 'inline';
            setMode(modePreference);
          }}
          options={[
            { value: 'split', label: 'Side by side' },
            { value: 'inline', label: 'Inline' },
          ]}
        />
        <Dropdown trigger={['click']} menu={menu}>
          <Button size="small" type="text" icon={<MoreOutlined />} aria-label="More comparison actions" />
        </Dropdown>
      </Flex>
      {body}
    </Flex>
  );
}
