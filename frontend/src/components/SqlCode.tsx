import React, { useMemo } from 'react';
import { highlightSql, SqlTokenKind } from '../sqlHighlight';
import { Tokens, useTokens } from './primitives';

/** How each kind of token is drawn - from the host's theme tokens, so it reads in light and dark. */
export function tokenStyles(token: Tokens): Record<SqlTokenKind, React.CSSProperties> {
  const named = token as unknown as Record<string, string>;
  return {
    keyword: { color: token.colorPrimaryText, fontWeight: 600 },
    function: { color: named.purple ?? token.colorPrimary },
    type: { color: named.cyan ?? token.colorInfoText },
    string: { color: token.colorErrorText },
    // Not the warning colour, a yellow that's hard to read on white.
    number: { color: token.colorSuccessText },
    comment: { color: token.colorTextTertiary, fontStyle: 'italic' },
    identifier: { color: token.colorText },
    quoted: { color: named.magenta ?? token.colorText },
    operator: { color: token.colorTextSecondary },
    punctuation: { color: token.colorTextSecondary },
    space: {},
  };
}

/** One line's part in a block of lines drawn together - a section of the query. */
export interface LineBlock {
  colour: string;
  /** The block's first and last lines get its rounded ends, and a little space outside them. */
  first: boolean;
  last: boolean;
  /** Outlined - the block being hovered or kept lit. */
  strong: boolean;
  /** A label in a circle beside the block's first line (its number), or null. */
  badge: string | null;
}

export interface SqlCodeProps {
  sql: string;
  /** Per line (0-based): the block it belongs to, or null. */
  block?: (line: number) => LineBlock | null;
  /** Per line (0-based): whether it has changed in the editor since - marked with a bar in the margin. */
  changed?: (line: number) => boolean;
  /** Draws the blocks greyed and hatched: what they describe is out of date, or being redone. */
  faded?: boolean;
  /** A bar running along the top: something is being worked out for this SQL right now. */
  refreshing?: boolean;
  onLineEnter?: (line: number) => void;
  onLeave?: () => void;
  onLineClick?: (line: number) => void;
  /** Lets the caller scroll a line into view: `data-line` is set on each line's element. */
  containerRef?: React.Ref<HTMLDivElement>;
  style?: React.CSSProperties;
}

const BADGE = 22;

/** `hex` at `alpha` (0-255); other colour forms are returned as they are. */
function tint(hex: string, alpha: number): string {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return hex;
  return `${hex}${Math.round(alpha).toString(16).padStart(2, '0')}`;
}

const OUTLINE = 2;

/** The line's part of its block's outline when the block is `strong`, plus the margin bar marking a
 * changed line - inset shadows, so outlining a block never moves its text. */
function lineShadow(own: LineBlock | null, changedColour: string | false, faded?: boolean): string | undefined {
  const shadows: string[] = [];
  if (changedColour) shadows.push(`inset 3px 0 0 ${changedColour}`);
  if (own?.strong && !faded) {
    shadows.push(`inset ${OUTLINE}px 0 0 ${own.colour}`, `inset -${OUTLINE}px 0 0 ${own.colour}`);
    if (own.first) shadows.push(`inset 0 ${OUTLINE}px 0 ${own.colour}`);
    if (own.last) shadows.push(`inset 0 -${OUTLINE}px 0 ${own.colour}`);
  }
  return shadows.length ? shadows.join(', ') : undefined;
}

/**
 * SQL drawn read-only with line numbers and syntax colouring, one element per line - and, given
 * `block`, its sections as tinted, rounded blocks, each numbered beside its first line, the hovered
 * or kept-lit one outlined. `changed` marks lines that differ from the editor's SQL now; `faded`
 * greys the whole thing out (out of date); `refreshing` runs a bar along the top while its
 * replacement is being made.
 */
const KEYFRAMES = `@keyframes nlsql-sweep { 0% { left: -35%; } 100% { left: 100%; } }`;

export function SqlCode({ sql, block, changed, faded, refreshing, onLineEnter, onLeave, onLineClick, containerRef, style }: SqlCodeProps) {
  const token = useTokens();
  const hatch = `repeating-linear-gradient(135deg, ${token.colorFillSecondary} 0 5px, ${token.colorFillQuaternary} 5px 10px)`;
  const lines = useMemo(() => highlightSql(sql), [sql]);
  const styles = useMemo(() => tokenStyles(token), [token]);
  const gutter = String(lines.length).length + 1;
  const hasBlocks = block !== undefined;
  const gap = token.marginXXS;
  return (
    <div
      ref={containerRef}
      onMouseLeave={onLeave}
      style={{
        fontFamily: token.fontFamilyCode,
        fontSize: token.fontSizeSM,
        lineHeight: 1.7,
        overflow: 'auto',
        background: token.colorBgContainer,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadiusLG,
        position: 'relative',
        ...style,
      }}
    >
      <style>{KEYFRAMES}</style>
      {refreshing && (
        <div style={{ position: 'sticky', top: 0, left: 0, height: 0, zIndex: 1 }}>
          <div style={{ position: 'relative', height: 2, overflow: 'hidden' }}>
            <div
              style={{
                position: 'absolute',
                top: 0,
                height: 2,
                width: '35%',
                background: token.colorPrimary,
                animation: 'nlsql-sweep 1.4s ease-in-out infinite',
              }}
            />
          </div>
        </div>
      )}
      <div style={{ display: 'inline-block', minWidth: '100%', padding: `${token.paddingXS}px ${token.paddingXS}px` }}>
        {lines.map((parts, index) => {
          const own = block?.(index) ?? null;
          const isChanged = changed?.(index) ?? false;
          const radius = token.borderRadiusLG;
          // The block's padding, on every column so the line number stays level with its code.
          const padding = {
            paddingTop: own?.first ? token.paddingXXS : 0,
            paddingBottom: own?.last ? token.paddingXXS : 0,
          };
          return (
            <div
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              data-line={index}
              onMouseEnter={() => onLineEnter?.(index)}
              onClick={() => onLineClick?.(index)}
              style={{
                display: 'flex',
                alignItems: 'stretch',
                whiteSpace: 'pre',
                marginTop: own?.first && index > 0 ? gap : 0,
                marginBottom: own?.last ? gap : 0,
                cursor: onLineClick && own ? 'pointer' : undefined,
              }}
            >
              <span
                style={{
                  width: `${gutter}ch`,
                  flexShrink: 0,
                  textAlign: 'right',
                  paddingRight: token.paddingXS,
                  color: token.colorTextQuaternary,
                  userSelect: 'none',
                  ...padding,
                }}
              >
                {index + 1}
              </span>
              {hasBlocks && (
                <span style={{ width: BADGE + token.paddingSM, flexShrink: 0, position: 'relative', userSelect: 'none' }}>
                  {own?.badge && (
                    <span
                      style={{
                        position: 'absolute',
                        top: 1 + padding.paddingTop,
                        left: token.paddingXXS,
                        width: BADGE,
                        height: BADGE,
                        borderRadius: BADGE / 2,
                        background: faded ? token.colorTextQuaternary : own.colour,
                        color: '#fff',
                        fontFamily: token.fontFamily,
                        fontSize: token.fontSizeSM,
                        fontWeight: 600,
                        lineHeight: `${BADGE}px`,
                        textAlign: 'center',
                        boxShadow: own.strong ? `0 0 0 3px ${tint(own.colour, 70)}` : undefined,
                      }}
                    >
                      {own.badge}
                    </span>
                  )}
                </span>
              )}
              <span
                style={{
                  flex: 1,
                  paddingLeft: token.paddingSM,
                  paddingRight: token.paddingSM,
                  ...padding,
                  background: own ? (faded ? hatch : tint(own.colour, 64)) : undefined,
                  color: faded ? token.colorTextSecondary : undefined,
                  boxShadow: lineShadow(own, isChanged && token.colorWarning, faded),
                  borderTopLeftRadius: own?.first ? radius : 0,
                  borderTopRightRadius: own?.first ? radius : 0,
                  borderBottomLeftRadius: own?.last ? radius : 0,
                  borderBottomRightRadius: own?.last ? radius : 0,
                  transition: 'box-shadow 120ms ease',
                }}
              >
                {parts.length === 0
                  ? ' '
                  : parts.map((part, partIndex) => (
                      // eslint-disable-next-line react/no-array-index-key
                      <span key={partIndex} style={faded ? undefined : styles[part.kind]}>
                        {part.text}
                      </span>
                    ))}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
