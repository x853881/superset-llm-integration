import React, { useMemo } from 'react';
import { Button, Flex, Tooltip, Typography } from 'antd';
import DiffOutlined from '@ant-design/icons/DiffOutlined';
import SyncOutlined from '@ant-design/icons/SyncOutlined';
import { lineDiff } from '../lineDiff';
import { useWording } from '../verbosity';
import { useTokens } from './primitives';

/**
 * Whether a result still describes the SQL in the editor: `current` when it does, `stale` once the
 * editor has moved on, `refreshing` while a new one is being made to replace it. The same three
 * states, drawn the same way, for every result the assistant keeps beside the SQL it was made from -
 * the breakdown, the description, a faster version, a plan's explanation.
 */
export type Freshness = 'current' | 'stale' | 'refreshing';

export function freshnessOf(stale: boolean, refreshing: boolean): Freshness {
  if (refreshing) return 'refreshing';
  return stale ? 'stale' : 'current';
}

/** How the SQL a result was made from differs from the editor's now. */
export interface LineChanges {
  /** 0-based lines of the *old* SQL that are gone or changed, or next to which lines were added. */
  lines: Set<number>;
  /** Roughly how many lines changed - a changed line counts once, not as a removal plus an addition. */
  count: number;
}

/** `madeFrom` against `current`, line by line (re-indenting isn't a change); null when either is
 * too long to diff here or they're the same. */
export function lineChanges(madeFrom: string, current: string): LineChanges | null {
  if (madeFrom === current) return null;
  const diff = lineDiff(madeFrom, current);
  if (diff === null) return null;
  const lines = new Set<number>();
  let oldLine = 0;
  let removed = 0;
  let added = 0;
  diff.forEach(entry => {
    if (entry.kind === 'same') {
      oldLine += 1;
    } else if (entry.kind === 'removed') {
      lines.add(oldLine);
      removed += 1;
      oldLine += 1;
    } else {
      // An added line has no line of its own in the old SQL: mark the one it went in after.
      lines.add(Math.max(0, oldLine - 1));
      added += 1;
    }
  });
  const count = Math.max(removed, added);
  return count === 0 ? null : { lines, count };
}

export function useLineChanges(madeFrom: string | null, current: string): LineChanges | null {
  return useMemo(() => (madeFrom === null ? null : lineChanges(madeFrom, current)), [madeFrom, current]);
}

export interface StaleNoticeProps {
  freshness: Freshness;
  /** What the result is, for the tooltip: "breakdown", "description", "faster version", "plan". */
  what: string;
  /** How many lines of the editor's SQL have changed since, when known. */
  changedLines?: number | null;
  /** How long the refresh has been running. */
  elapsedMs?: number;
  /** Makes a new one for the editor's SQL now; omitted where there's no single action to offer. */
  onRefresh?: () => void;
  refreshDisabled?: boolean;
}

/**
 * A small chip saying a result is out of date - and by how much - or being brought up to date.
 * Nothing when it's current. Deliberately small: an out-of-date result is still worth reading (it
 * says what the SQL *was*), so this marks it rather than covering it.
 */
export function StaleNotice({ freshness, what, changedLines, elapsedMs, onRefresh, refreshDisabled }: StaleNoticeProps) {
  const token = useTokens();
  const say = useWording();
  if (freshness === 'current') return null;
  const refreshing = freshness === 'refreshing';
  const label = refreshing
    ? `Updating${elapsedMs !== undefined ? ` · ${(elapsedMs / 1000).toFixed(0)} s` : ''}`
    : changedLines
      ? `Out of date · ${changedLines} line${changedLines === 1 ? '' : 's'} changed`
      : 'Out of date';
  const tooltip = refreshing
    ? say(`A new ${what} is being written for the SQL in the editor. This one stays until it's ready.`, `Writing a new ${what}.`)
    : say(
        `The SQL in the editor has changed since this ${what} was made - it describes the SQL it was made from.`,
        'Made from earlier SQL.',
      );
  return (
    <Flex align="center" gap={token.marginXXS} style={{ flexShrink: 0 }}>
      <Tooltip title={tooltip}>
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: token.marginXXS,
            padding: `0 ${token.paddingXS}px`,
            lineHeight: `${token.controlHeightSM}px`,
            borderRadius: token.borderRadiusSM,
            fontSize: token.fontSizeSM,
            whiteSpace: 'nowrap',
            // Not the warning colour for the text: it's a yellow that's hard to read (see `toneColours`).
            color: refreshing ? token.colorInfoText : token.colorText,
            background: refreshing ? token.colorInfoBg : token.colorWarningBg,
            border: `1px solid ${refreshing ? token.colorInfoBorder : token.colorWarningBorder}`,
          }}
        >
          {refreshing ? <SyncOutlined spin /> : <DiffOutlined />}
          <Typography.Text style={{ color: 'inherit', fontSize: 'inherit' }}>{label}</Typography.Text>
        </span>
      </Tooltip>
      {!refreshing && onRefresh && (
        <Button size="small" type="link" disabled={refreshDisabled} onClick={onRefresh} style={{ paddingInline: token.paddingXXS }}>
          Refresh
        </Button>
      )}
    </Flex>
  );
}

/**
 * A result drawn as out of date while its replacement is being made: still readable, but plainly
 * not the thing to act on - dimmed, and not clickable. Fades the replacement in when it lands.
 */
export function Refreshable({ refreshing, children }: { refreshing: boolean; children: React.ReactNode }) {
  return (
    <div
      aria-busy={refreshing}
      style={{
        opacity: refreshing ? 0.5 : 1,
        pointerEvents: refreshing ? 'none' : undefined,
        transition: 'opacity 240ms ease',
      }}
    >
      {children}
    </div>
  );
}
