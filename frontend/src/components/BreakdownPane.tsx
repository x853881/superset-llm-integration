import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dropdown, Flex, Spin, Switch, Tag, Tooltip, Typography } from 'antd';
import type { MenuProps } from 'antd';
import AlignLeftOutlined from '@ant-design/icons/AlignLeftOutlined';
import DownOutlined from '@ant-design/icons/DownOutlined';
import LoadingOutlined from '@ant-design/icons/LoadingOutlined';
import MoreOutlined from '@ant-design/icons/MoreOutlined';
import PartitionOutlined from '@ant-design/icons/PartitionOutlined';
import ProfileOutlined from '@ant-design/icons/ProfileOutlined';
import { components } from '@apache-superset/core';
import { breakdownSource, runBreakdown } from '../breakdownAction';
import { useFillHeight, useVisible } from '../estimate';
import type { BreakdownResult, BreakdownSection, BreakdownTable } from '../performanceResult';
import { useActiveSqlLabTab, useTabEditor } from '../sqlLabHooks';
import { useWording } from '../verbosity';
import {
  breakdownFor,
  isSqlEmpty,
  pushEdit,
  recordManualEditIfAny,
  rememberBreakdown,
  stopCall,
  updateTabState,
  useTabState,
} from '../tabState';
import { ProgressPanel, useElapsedMs } from './ProgressPanel';
import { FeedbackBar } from './FeedbackBar';
import { PALETTE, sectionAt, withAlpha } from './SqlBreakdown';
import { SqlCode } from './SqlCode';
import { freshnessOf, Refreshable, StaleNotice, useLineChanges } from './StaleNotice';
import { Flag, useTokens } from './primitives';
import { plainText, Prose } from './Prose';

/** Whether new breakdowns lay the SQL out first - one choice for the session, not per tab. */
let formatPreference = true;
/** Whether the cards show their detailed explanation - likewise. */
let detailPreference = false;

/** How often the editor's cursor is looked at, to light up the section it's in. The extension API
 * reports content changes but not cursor moves, so it's read on a timer while the pane is on screen. */
const CURSOR_POLL_MS = 400;

/**
 * The "Break it down" tab under SQL Lab's editor, beside Results and Query history: the query's
 * intent across the top; the query on the left - laid out one clause per line and syntax-coloured,
 * each section a tinted block numbered beside its first line - and on the right a card per section
 * saying what it does, the tables it reads, and a fuller explanation on request. Hover either side
 * to light up the other; click a section to keep it lit (and, when the SQL shown is the editor's own,
 * select it there); move the cursor in the editor and the section it's in lights up here.
 *
 * Out of date, it says so - and by how much - with the lines that have changed since marked in the
 * margin; being redone, the old breakdown stays readable, greyed, until the new one's sections
 * arrive, which they do one at a time as the model writes them.
 *
 * The same breakdown the side panel's Describe tab shows, from the same per-tab state: start it from
 * either and it appears in both.
 */
export function BreakdownPane() {
  const token = useTokens();
  const say = useWording();
  const activeTab = useActiveSqlLabTab();
  const { editor, sql, resync } = useTabEditor(activeTab);
  const [state] = useTabState(activeTab?.id ?? '');
  const [formatFirst, setFormatFirst] = useState(formatPreference);
  const [detailed, setDetailed] = useState(detailPreference);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [hovered, setHovered] = useState<number | null>(null);
  const [pinned, setPinned] = useState<number | null>(null);
  const [cursorLine, setCursorLine] = useState<number | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const codeRef = useRef<HTMLDivElement>(null);
  const cardsRef = useRef<HTMLDivElement>(null);
  const elapsed = useElapsedMs(state.nlCall?.startedAt ?? null);
  const [root, setRoot] = useState<HTMLElement | null>(null);
  const visible = useVisible(root);
  const fillHeight = useFillHeight(visible ? root : null);

  const source = breakdownSource(sql);
  const hasSql = !isSqlEmpty(source);
  const entry = breakdownFor(state, source);
  const running = state.nlCall?.kind === 'breakdown' ? state.nlCall : null;
  const otherRunning = state.generatingNl && !running;

  // While a breakdown is being written, what's settled of it so far: the SQL its sections refer to,
  // then the sections one by one. Shown in place of the old breakdown from its first section on.
  const partial = running?.progress.partial;
  const partialSql = partial?.sql as { sql: string; formatted: boolean } | undefined;
  const partialSections = (partial?.sections as BreakdownSection[] | undefined) ?? [];
  const streaming = running !== null && partialSql !== undefined && partialSections.length > 0;
  const shown: BreakdownResult | null = streaming
    ? {
        summary: running.progress.text.summary ?? '',
        sections: partialSections,
        sql: partialSql.sql,
        formatted: partialSql.formatted,
      }
    : entry?.result ?? null;

  const stale = entry !== null && entry.sql !== source;
  const refreshing = running !== null && !streaming && entry !== null;
  const freshness = streaming ? 'current' : freshnessOf(stale, refreshing);
  const shownSql = streaming ? partialSql.sql : entry ? entry.result.sql ?? entry.sql : source;
  const sections = shown?.sections ?? [];
  /** A breakdown written with Brief on has no detailed explanations to show. */
  const briefOnly = shown?.brief === true;
  const isCurrent = entry !== null && !stale && !streaming && running === null;
  const canSelectInEditor = isCurrent && !entry.result.formatted && editor !== null;
  const canApplyLayout = isCurrent && entry.result.formatted === true && editor !== null && !state.generatingSql;
  // Changed lines can only be marked on SQL that is the editor's own (not the backend's layout of it).
  const changes = useLineChanges(entry && !streaming ? entry.sql : null, source);
  const markChanges = stale && !entry?.result.formatted && !refreshing;

  const colours = useMemo(
    () =>
      sections.map(
        (_, index) => (token as unknown as Record<string, string>)[PALETTE[index % PALETTE.length]] ?? token.colorPrimary,
      ),
    [sections, token],
  );
  const lineCount = shownSql.split('\n').length;
  const lineOwner = useMemo(
    () => Array.from({ length: lineCount }, (_, index) => sectionAt(sections, index + 1)),
    [lineCount, sections],
  );
  const cursorSection = cursorLine === null ? null : lineOwner[cursorLine] ?? null;
  const active = hovered ?? pinned ?? cursorSection;

  // The editor's cursor, while the breakdown is of exactly the editor's SQL: the section it's in is
  // lit, and its card scrolled into view, as the cursor moves.
  useEffect(() => {
    if (!visible || !canSelectInEditor || !editor) {
      setCursorLine(null);
      return undefined;
    }
    const read = () => {
      try {
        const position = editor.getCursorPosition();
        setCursorLine(previous => (previous === position.line ? previous : position.line));
      } catch {
        setCursorLine(null);
      }
    };
    read();
    const handle = window.setInterval(read, CURSOR_POLL_MS);
    return () => window.clearInterval(handle);
  }, [visible, canSelectInEditor, editor]);

  useEffect(() => {
    if (cursorSection === null || hovered !== null) return;
    reveal(cardsRef.current, `[data-section="${cursorSection}"]`);
  }, [cursorSection, hovered]);

  if (!activeTab) {
    return (
      <div ref={setRoot}>
        <Typography.Text type="secondary">Open a SQL Lab tab to break its query down.</Typography.Text>
      </div>
    );
  }

  const start = () => {
    formatPreference = formatFirst;
    setPinned(null);
    setExpanded(new Set());
    runBreakdown(activeTab, sql, formatFirst, state.generatingNl);
  };

  const focusSection = (index: number | null, from: 'code' | 'card') => {
    setHovered(index);
    if (index === null) return;
    if (from === 'card') reveal(codeRef.current, `[data-line="${sections[index].startLine - 1}"]`);
    else reveal(cardsRef.current, `[data-section="${index}"]`);
  };

  const selectInEditor = (section: BreakdownSection) => {
    if (!canSelectInEditor || !editor) return;
    const lines = sql.split('\n');
    const endLine = Math.min(section.endLine, lines.length);
    try {
      editor.setSelection({
        start: { line: section.startLine - 1, column: 0 },
        end: { line: endLine - 1, column: lines[endLine - 1]?.length ?? 0 },
      });
      editor.scrollToLine(section.startLine - 1);
    } catch {
      // An editor that can't select just doesn't.
    }
  };

  const copy = (what: 'sql' | 'markdown') => {
    const text = what === 'sql' ? shownSql : breakdownMarkdown(shown, shownSql);
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(what);
      window.setTimeout(() => setCopied(null), 2000);
    });
  };

  // Puts the backend's layout of the query into the editor - a step in the undo timeline like any
  // other write - after which the breakdown is of the editor's own SQL, so its sections can be
  // selected there and followed by the cursor.
  const applyLayout = () => {
    if (!canApplyLayout || !editor || !entry?.result.sql) return;
    const layout = entry.result.sql;
    const targetId = activeTab.id;
    let current: string;
    try {
      current = editor.getValue();
    } catch {
      return;
    }
    recordManualEditIfAny(targetId, current);
    pushEdit(targetId, { type: 'manual', command: 'Laid out by Break it down', oldSql: current, newSql: layout, result: null });
    if (current !== layout) editor.setValue(layout);
    resync();
    updateTabState(targetId, prev => ({
      ...rememberBreakdown(prev, { result: { ...entry.result, formatted: false }, sql: breakdownSource(layout) }),
      lastAppliedSql: layout,
    }));
  };

  const menu: MenuProps = {
    items: [
      { key: 'format', label: `${formatFirst ? '✓ ' : ''}Lay the SQL out first` },
      { type: 'divider' },
      { key: 'copySql', label: 'Copy SQL', disabled: isSqlEmpty(shownSql) },
      { key: 'copyMarkdown', label: 'Copy as Markdown', disabled: !shown },
      { key: 'applyLayout', label: 'Use this layout in the editor', disabled: !canApplyLayout },
    ],
    onClick: ({ key }) => {
      if (key === 'format') {
        setFormatFirst(previous => {
          formatPreference = !previous;
          return !previous;
        });
      } else if (key === 'copySql') copy('sql');
      else if (key === 'copyMarkdown') copy('markdown');
      else if (key === 'applyLayout') applyLayout();
    },
  };

  const status = otherRunning
    ? say('The assistant panel is describing this query - one description at a time.', 'Busy describing it in the assistant panel.')
    : shown
      ? `${sections.length} section${sections.length === 1 ? '' : 's'}${shown.formatted ? ' · laid out by the assistant' : ''}`
      : hasSql
        ? say('The SQL in the editor, section by section, explained beside it.', "The editor's SQL, part by part.")
        : say('Write or paste a query in the editor to break it down.', 'Write some SQL to break down.');

  return (
    <Flex
      ref={setRoot}
      vertical
      gap={token.marginXS}
      style={{ height: fillHeight ?? 'auto', minHeight: 240, padding: `0 ${token.paddingXS}px` }}
    >
      <Flex align="center" gap={token.marginSM} wrap="wrap" style={{ flexShrink: 0 }}>
        <Button
          type="primary"
          icon={<PartitionOutlined />}
          disabled={!hasSql || state.generatingNl}
          loading={running !== null}
          onClick={start}
        >
          {entry ? 'Break it down again' : 'Break it down'}
        </Button>
        <StaleNotice freshness={freshness} what="breakdown" changedLines={changes?.count} elapsedMs={elapsed} />
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM, flex: 1, minWidth: 120 }} ellipsis>
          {copied ? `Copied ${copied === 'sql' ? 'the SQL' : 'the breakdown as Markdown'}` : status}
        </Typography.Text>
        {canApplyLayout && (
          <Tooltip
            title={say(
              'Puts this layout of the query into the editor (undoable), so its sections can be selected there.',
              'Put this layout in the editor (undoable)',
            )}
          >
            <Button size="small" icon={<AlignLeftOutlined />} onClick={applyLayout}>
              Use this layout
            </Button>
          </Tooltip>
        )}
        <Dropdown trigger={['click']} menu={menu}>
          <Button size="small" type="text" icon={<MoreOutlined />} aria-label="More breakdown actions" />
        </Dropdown>
      </Flex>

      {shown?.summary && (
        <Refreshable refreshing={refreshing}>
          <Flex
            gap={token.marginSM}
            style={{
              flexShrink: 0,
              padding: `${token.paddingSM}px ${token.paddingMD}px`,
              borderRadius: token.borderRadiusLG,
              background: refreshing ? token.colorFillQuaternary : token.colorPrimaryBg,
              border: `1px solid ${refreshing ? token.colorBorderSecondary : token.colorPrimaryBorder}`,
            }}
          >
            <ProfileOutlined
              style={{ color: refreshing ? token.colorTextTertiary : token.colorPrimary, fontSize: token.fontSizeLG + 2, marginTop: 2 }}
            />
            <Flex vertical gap={token.marginXXS} style={{ minWidth: 0 }}>
              <Typography.Text strong style={{ fontSize: token.fontSizeLG }}>
                Query intent
              </Typography.Text>
              <Typography.Text type="secondary">
                <Prose text={shown.summary} />
              </Typography.Text>
            </Flex>
          </Flex>
        </Refreshable>
      )}

      {state.error && entry === null && !running && (
        <components.Alert
          type="error"
          showIcon
          closable
          onClose={() => updateTabState(activeTab.id, prev => ({ ...prev, error: '' }))}
        >
          {state.error}
        </components.Alert>
      )}

      <Flex gap={token.marginSM} style={{ flex: 1, minHeight: 0 }}>
        <SqlCode
          // A new breakdown landing fades in rather than snapping over the old one (not each section
          // as it streams in, which would also throw away the scroll position every time).
          key={streaming ? 'streaming' : entry ? `${entry.sql.length}:${entry.result.summary.slice(0, 40)}` : 'none'}
          sql={shownSql}
          containerRef={codeRef}
          style={{ flex: '1 1 55%', minWidth: 0, maxHeight: '100%', animation: 'nlsql-fade 240ms ease' }}
          faded={refreshing}
          refreshing={running !== null}
          changed={markChanges && changes ? line => changes.lines.has(line) : undefined}
          block={
            sections.length === 0
              ? undefined
              : line => {
                  const owner = lineOwner[line];
                  if (owner === null || owner === undefined) return null;
                  const first = line === 0 || lineOwner[line - 1] !== owner;
                  return {
                    colour: colours[owner],
                    first,
                    last: line === lineCount - 1 || lineOwner[line + 1] !== owner,
                    strong: active === owner,
                    badge: first && line + 1 === sections[owner].startLine ? String(owner + 1) : null,
                  };
                }
          }
          onLineEnter={line => focusSection(lineOwner[line] ?? null, 'code')}
          onLeave={() => setHovered(null)}
          onLineClick={line => {
            const owner = lineOwner[line];
            if (owner === null || owner === undefined) return;
            setPinned(pinned === owner ? null : owner);
            selectInEditor(sections[owner]);
          }}
        />

        <div ref={cardsRef} style={{ flex: '1 1 45%', minWidth: 0, overflowY: 'auto', maxHeight: '100%' }}>
          <style>{'@keyframes nlsql-fade { from { opacity: 0.35; } to { opacity: 1; } }'}</style>
          {running && !streaming && (
            <ProgressPanel kind="breakdown" progress={running.progress} elapsedMs={elapsed} onStop={() => stopCall(running)} />
          )}
          {running && streaming && (
            <Flex align="center" gap={token.marginXS} style={{ padding: `${token.paddingXXS}px 0 ${token.paddingXS}px` }}>
              <Spin indicator={<LoadingOutlined spin />} size="small" />
              <Typography.Text type="secondary" style={{ flex: 1, fontSize: token.fontSizeSM }}>
                Writing the breakdown · {(elapsed / 1000).toFixed(0)} s
              </Typography.Text>
              <Button size="small" onClick={() => stopCall(running)}>
                Stop
              </Button>
            </Flex>
          )}
          {!shown && !running ? (
            <Flex vertical gap={token.marginXS} style={{ padding: token.paddingSM }}>
              <Typography.Text type="secondary">
                Press <Typography.Text strong>Break it down</Typography.Text>
                {say(
                  ' and the assistant explains the query part by part - each CTE, the sources and joins, the filters, the grouping - with each part marked in the SQL beside it.',
                  ' to explain the query part by part.',
                )}
              </Typography.Text>
              {say(
                <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                  Nothing is run, and the editor isn't changed.
                </Typography.Text>,
                null,
              )}
            </Flex>
          ) : shown ? (
            <Refreshable refreshing={refreshing}>
              <Flex vertical gap={token.marginSM} onMouseLeave={() => setHovered(null)} style={{ marginTop: refreshing ? token.marginSM : 0 }}>
                <Flex
                  align="center"
                  justify="space-between"
                  style={{
                    padding: `${token.paddingXS}px ${token.paddingMD}px`,
                    borderRadius: token.borderRadiusLG,
                    border: `1px solid ${token.colorBorderSecondary}`,
                  }}
                >
                  <Flex vertical>
                    <Typography.Text strong>Show detailed explanation</Typography.Text>
                    {briefOnly && (
                      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                        {say(
                          "Written with Brief on, so there isn't one. Untick Brief and break it down again to get it.",
                          'None - written with Brief on.',
                        )}
                      </Typography.Text>
                    )}
                  </Flex>
                  <Switch
                    checked={detailed}
                    disabled={briefOnly}
                    onChange={checked => {
                      detailPreference = checked;
                      setDetailed(checked);
                      setExpanded(new Set());
                    }}
                  />
                </Flex>
                {sections.length === 0 && !streaming && (
                  <Typography.Text type="secondary">The model didn't divide this query into sections.</Typography.Text>
                )}
                {sections.map((section, index) => (
                  <SectionCard
                    // eslint-disable-next-line react/no-array-index-key
                    key={index}
                    index={index}
                    section={section}
                    colour={refreshing ? token.colorTextQuaternary : colours[index]}
                    active={active === index}
                    pinned={pinned === index}
                    open={detailed !== expanded.has(index)}
                    onToggle={() =>
                      setExpanded(previous => {
                        const next = new Set(previous);
                        if (next.has(index)) next.delete(index);
                        else next.add(index);
                        return next;
                      })
                    }
                    onEnter={() => focusSection(index, 'card')}
                    onClick={() => {
                      setPinned(pinned === index ? null : index);
                      selectInEditor(section);
                    }}
                  />
                ))}
                {streaming && <WritingCard index={sections.length} />}
                {!streaming && !running && <FeedbackBar trace={shown.trace} target="breakdown" />}
              </Flex>
            </Refreshable>
          ) : null}
        </div>
      </Flex>
    </Flex>
  );
}

function reveal(container: HTMLDivElement | null, selector: string) {
  const element = container?.querySelector(selector) as HTMLElement | null;
  element?.scrollIntoView?.({ block: 'nearest' });
}

/** A breakdown as Markdown - the intent, each section with its lines, explanation and tables, then
 * the SQL itself - for pasting into a review or a ticket. */
export function breakdownMarkdown(result: BreakdownResult | null, sql: string): string {
  if (!result) return '';
  const out: string[] = [];
  if (result.summary) out.push('## Query intent', '', plainText(result.summary), '');
  result.sections.forEach((section, index) => {
    const range = section.startLine === section.endLine ? `line ${section.startLine}` : `lines ${section.startLine}–${section.endLine}`;
    out.push(`### ${index + 1}. ${section.title} (${range})`, '');
    if (section.description) out.push(plainText(section.description), '');
    if (section.detail) out.push(plainText(section.detail), '');
    if (section.tables?.length) out.push(`Reads: ${section.tables.map(tableLabel).join(', ')}`, '');
    (section.hints ?? []).forEach(hint => out.push(`> ⚠ ${hint}`, ''));
  });
  out.push('```sql', sql.replace(/\s+$/, ''), '```', '');
  return out.join('\n');
}

function tableLabel(table: BreakdownTable): string {
  const name = `${table.schema}.${table.name}`;
  const rows = table.rows !== null && table.rows !== undefined ? ` (≈${compactNumber(table.rows)} rows)` : '';
  return `${name}${table.kind === 'view' ? ' (view)' : ''}${rows}`;
}

function compactNumber(value: number): string {
  const units: [number, string][] = [
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'k'],
  ];
  for (const [size, suffix] of units) {
    if (value >= size) return `${(value / size).toFixed(1).replace(/\.0$/, '')}${suffix}`;
  }
  return String(Math.round(value));
}

/** The next card while the model is still writing it. */
function WritingCard({ index }: { index: number }) {
  const token = useTokens();
  return (
    <Flex
      align="center"
      gap={token.marginSM}
      style={{
        padding: `${token.paddingSM}px ${token.paddingMD}px`,
        borderRadius: token.borderRadiusLG,
        border: `1px dashed ${token.colorBorder}`,
      }}
    >
      <Spin indicator={<LoadingOutlined spin />} size="small" />
      <Typography.Text type="secondary">Writing section {index + 1}…</Typography.Text>
    </Flex>
  );
}

function SectionCard({
  index,
  section,
  colour,
  active,
  pinned,
  open,
  onToggle,
  onEnter,
  onClick,
}: {
  index: number;
  section: BreakdownSection;
  colour: string;
  active: boolean;
  pinned: boolean;
  /** Whether the detailed explanation shows. */
  open: boolean;
  onToggle: () => void;
  onEnter: () => void;
  onClick: () => void;
}) {
  const token = useTokens();
  const range = section.startLine === section.endLine ? `Line ${section.startLine}` : `Lines ${section.startLine}–${section.endLine}`;
  const detail = section.detail?.trim() ?? '';
  const tables = section.tables ?? [];
  const hints = section.hints ?? [];
  return (
    <div
      data-section={index}
      role="button"
      tabIndex={0}
      onMouseEnter={onEnter}
      onFocus={onEnter}
      onClick={onClick}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onClick();
        }
      }}
      style={{
        display: 'flex',
        gap: token.marginSM,
        cursor: 'pointer',
        padding: `${token.paddingSM}px ${token.paddingMD}px`,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${active ? colour : token.colorBorderSecondary}`,
        boxShadow: pinned ? `0 0 0 1px ${colour}` : undefined,
        background: active ? withAlpha(colour, 22) : token.colorBgContainer,
        transition: 'background 120ms ease, border-color 120ms ease',
        animation: 'nlsql-fade 240ms ease',
      }}
    >
      <div
        style={{
          flexShrink: 0,
          width: 28,
          height: 28,
          borderRadius: 14,
          background: colour,
          color: '#fff',
          fontSize: token.fontSize,
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {index + 1}
      </div>
      <Flex vertical gap={token.marginXXS} style={{ minWidth: 0, flex: 1 }}>
        <Flex align="center" gap={token.marginXS}>
          <Typography.Text strong style={{ flex: 1, minWidth: 0, fontSize: token.fontSizeLG }} ellipsis={{ tooltip: section.title }}>
            {section.title}
          </Typography.Text>
          <Tag style={{ marginInlineEnd: 0, whiteSpace: 'nowrap' }}>{range}</Tag>
          <Button
            type="text"
            size="small"
            aria-label={open ? 'Hide the detailed explanation' : 'Show the detailed explanation'}
            disabled={!detail}
            icon={<DownOutlined style={{ transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 120ms ease' }} />}
            onClick={event => {
              event.stopPropagation();
              onToggle();
            }}
          />
        </Flex>
        {section.description && (
          <Typography.Text type="secondary">
            <Prose text={section.description} />
          </Typography.Text>
        )}
        {tables.length > 0 && (
          <Flex gap={token.marginXXS} wrap="wrap">
            {tables.map(table => (
              <Tooltip
                key={`${table.schema}.${table.name}`}
                title={`${table.kind === 'view' ? 'View' : 'Table'} ${table.schema}.${table.name}${
                  table.rows !== null ? ` - about ${table.rows.toLocaleString()} rows, by the planner's estimate` : ''
                }`}
              >
                <Tag bordered={false} style={{ marginInlineEnd: 0, fontFamily: token.fontFamilyCode, fontSize: token.fontSizeSM - 1 }}>
                  {table.schema}.{table.name}
                  {table.kind === 'view' ? ' (view)' : ''}
                  {table.rows !== null ? ` ≈${compactNumber(table.rows)}` : ''}
                </Tag>
              </Tooltip>
            ))}
          </Flex>
        )}
        {hints.map(hint => (
          <Flag key={hint} tone="warning">
            {hint}
          </Flag>
        ))}
        {open && detail && (
          <Typography.Paragraph
            style={{
              marginBottom: 0,
              marginTop: token.marginXXS,
              paddingTop: token.paddingXS,
              borderTop: `1px solid ${token.colorBorderSecondary}`,
            }}
          >
            <Prose text={detail} />
          </Typography.Paragraph>
        )}
      </Flex>
    </div>
  );
}
