import React, { useMemo, useState } from 'react';
import { Flex, Tag, Tooltip, Typography } from 'antd';
import type { BreakdownResult, BreakdownSection } from '../performanceResult';
import { SectionLabel, useTokens } from './primitives';
import { Freshness, Refreshable, StaleNotice } from './StaleNotice';
import { Prose } from './Prose';

export const PALETTE = ['green', 'blue', 'purple', 'gold', 'cyan', 'magenta', 'volcano', 'geekblue', 'lime', 'orange'] as const;

export const KIND_LABELS: Record<string, string> = {
  cte: 'CTE',
  select: 'Columns',
  source: 'Source',
  join: 'Join',
  filter: 'Filter',
  aggregate: 'Grouping',
  window: 'Window',
  order: 'Order',
  limit: 'Limit',
  subquery: 'Subquery',
  other: 'Other',
};

/** `hex` at `alpha` (0-255) - for a tint behind highlighted lines that works on light and dark. */
export function withAlpha(hex: string, alpha: number): string {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return hex;
  return `${hex}${Math.round(alpha).toString(16).padStart(2, '0')}`;
}

/** The narrowest section covering `line` (1-based) - the most specific thing that line is part of. */
export function sectionAt(sections: BreakdownSection[], line: number): number | null {
  let best: number | null = null;
  sections.forEach((section, index) => {
    if (line < section.startLine || line > section.endLine) return;
    if (best === null) {
      best = index;
      return;
    }
    const current = sections[best];
    if (section.endLine - section.startLine < current.endLine - current.startLine) best = index;
  });
  return best;
}

export interface SqlBreakdownProps {
  result: BreakdownResult;
  /** The SQL the breakdown describes - drawn here read-only, since the editor may have moved on. */
  sql: string;
  /** Whether it still describes the editor's SQL (see `StaleNotice`). */
  freshness: Freshness;
  /** How many lines of the editor's SQL have changed since, when known. */
  changedLines?: number | null;
  /** Selects a section's lines in the real SQL editor; null when that isn't possible (the editor's
   * SQL is no longer the SQL described). */
  onSelectInEditor: ((section: BreakdownSection) => void) | null;
}

/**
 * The query, part by part: an overview, the SQL with each section marked in its own colour down the
 * gutter, and a card per section saying what it does. Hovering a card highlights its lines (and
 * hovering a line highlights its card); clicking one selects those lines in the SQL editor itself.
 *
 * A first step towards describing the query in the editor itself - hover a description, see the SQL
 * it is about light up there - for which this panel would need to draw into the host's editor, which
 * the extension API doesn't offer yet beyond setting a selection.
 */
export function SqlBreakdown({ result, sql, freshness, changedLines, onSelectInEditor }: SqlBreakdownProps) {
  const token = useTokens();
  const [active, setActive] = useState<number | null>(null);
  const lines = useMemo(() => sql.split('\n'), [sql]);
  const colours = useMemo(
    () => result.sections.map((_, index) => (token as unknown as Record<string, string>)[PALETTE[index % PALETTE.length]] ?? token.colorPrimary),
    [result.sections, token],
  );
  const lineOwner = useMemo(() => lines.map((_, index) => sectionAt(result.sections, index + 1)), [lines, result.sections]);
  const gutterWidth = String(lines.length).length;

  return (
    <Refreshable refreshing={freshness === 'refreshing'}>
    <Flex vertical gap={token.marginSM}>
      <StaleNotice freshness={freshness} what="breakdown" changedLines={changedLines} />
      {result.summary && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Overview</SectionLabel>
          <Typography.Text>
            <Prose text={result.summary} />
          </Typography.Text>
        </Flex>
      )}

      <div
        onMouseLeave={() => setActive(null)}
        style={{
          maxHeight: 300,
          overflow: 'auto',
          fontFamily: token.fontFamilyCode,
          fontSize: token.fontSizeSM - 1,
          lineHeight: 1.55,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: token.borderRadius,
          background: token.colorFillQuaternary,
        }}
      >
        {lines.map((line, index) => {
          const owner = lineOwner[index];
          const highlighted =
            active !== null && index + 1 >= result.sections[active].startLine && index + 1 <= result.sections[active].endLine;
          return (
            <div
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              onMouseEnter={() => setActive(owner)}
              style={{
                display: 'flex',
                whiteSpace: 'pre',
                background: highlighted && active !== null ? withAlpha(colours[active], 40) : undefined,
                borderLeft: `3px solid ${owner !== null ? colours[owner] : 'transparent'}`,
              }}
            >
              <span
                style={{
                  width: `${gutterWidth + 1}ch`,
                  flexShrink: 0,
                  textAlign: 'right',
                  paddingRight: token.paddingXXS,
                  color: token.colorTextQuaternary,
                  userSelect: 'none',
                }}
              >
                {index + 1}
              </span>
              <span style={{ paddingLeft: token.paddingXS, paddingRight: token.paddingXS }}>{line || ' '}</span>
            </div>
          );
        })}
      </div>

      <Flex vertical gap={token.marginXS}>
        <SectionLabel>Section by section</SectionLabel>
        {result.sections.length === 0 && (
          <Typography.Text type="secondary">The model didn't divide this query into sections.</Typography.Text>
        )}
        {result.sections.map((section, index) => {
          const isActive = active === index;
          const range = section.startLine === section.endLine ? `line ${section.startLine}` : `lines ${section.startLine}–${section.endLine}`;
          return (
            <Tooltip
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              title={onSelectInEditor ? 'Click to select these lines in the editor' : undefined}
              mouseEnterDelay={0.8}
            >
              <div
                role={onSelectInEditor ? 'button' : undefined}
                tabIndex={onSelectInEditor ? 0 : undefined}
                onMouseEnter={() => setActive(index)}
                onMouseLeave={() => setActive(null)}
                onFocus={() => setActive(index)}
                onClick={() => onSelectInEditor?.(section)}
                onKeyDown={event => {
                  if ((event.key === 'Enter' || event.key === ' ') && onSelectInEditor) {
                    event.preventDefault();
                    onSelectInEditor(section);
                  }
                }}
                style={{
                  cursor: onSelectInEditor ? 'pointer' : 'default',
                  borderLeft: `4px solid ${colours[index]}`,
                  borderRadius: token.borderRadius,
                  padding: `${token.paddingXS}px ${token.paddingSM}px`,
                  background: isActive ? withAlpha(colours[index], 28) : token.colorBgContainer,
                  borderTop: `1px solid ${isActive ? colours[index] : token.colorBorderSecondary}`,
                  borderRight: `1px solid ${isActive ? colours[index] : token.colorBorderSecondary}`,
                  borderBottom: `1px solid ${isActive ? colours[index] : token.colorBorderSecondary}`,
                  transition: 'background 120ms ease',
                }}
              >
                <Flex align="center" justify="space-between" gap={token.marginXS}>
                  <Typography.Text strong>{section.title}</Typography.Text>
                  <Flex align="center" gap={token.marginXXS} style={{ flexShrink: 0 }}>
                    <Tag style={{ marginInlineEnd: 0 }}>{KIND_LABELS[section.kind] ?? section.kind}</Tag>
                    <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                      {range}
                    </Typography.Text>
                  </Flex>
                </Flex>
                {section.description && (
                  <Typography.Text style={{ display: 'block', fontSize: token.fontSizeSM, marginTop: token.marginXXS }}>
                    <Prose text={section.description} />
                  </Typography.Text>
                )}
              </div>
            </Tooltip>
          );
        })}
      </Flex>
    </Flex>
    </Refreshable>
  );
}
