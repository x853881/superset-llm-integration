import React, { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Button, Flex, Typography } from 'antd';
import DownOutlined from '@ant-design/icons/DownOutlined';
import UpOutlined from '@ant-design/icons/UpOutlined';
import { lineDiff } from '../lineDiff';
import { useTokens } from './primitives';

/** A line diff of `before` against `after`, added lines green and removed lines red. */
export function DiffView({
  before,
  after,
  maxHeight = 280,
  changesOnly = false,
}: {
  before: string;
  after: string;
  maxHeight?: number | null;
  /** Only the lines removed and added, with a ⋯ where unchanged lines were left out - not the
   * whole query again with the changes marked in it. */
  changesOnly?: boolean;
}) {
  const token = useTokens();
  const diff = useMemo(() => {
    const lines = lineDiff(before, after);
    if (!lines || !changesOnly) return lines;
    const kept: typeof lines = [];
    lines.forEach((line, index) => {
      if (line.kind !== 'same') kept.push(line);
      else if (kept.length > 0 && kept[kept.length - 1].text !== '⋯' && lines.slice(index + 1).some(next => next.kind !== 'same')) {
        kept.push({ kind: 'same', text: '⋯' });
      }
    });
    return kept;
  }, [before, after, changesOnly]);
  if (!diff) return <Typography.Text type="secondary">Too long to show a diff here.</Typography.Text>;
  if (diff.length === 0) return <Typography.Text type="secondary">Only the spacing changed.</Typography.Text>;
  return (
    <div
      style={{
        maxHeight: maxHeight ?? undefined,
        overflow: maxHeight === null ? 'visible' : 'auto',
        fontFamily: token.fontFamilyCode,
        fontSize: token.fontSizeSM - 1,
        lineHeight: 1.5,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadius,
      }}
    >
      {diff.map((line, index) => (
        <div
          // eslint-disable-next-line react/no-array-index-key
          key={index}
          style={{
            whiteSpace: 'pre',
            padding: `0 ${token.paddingXS}px`,
            background:
              line.kind === 'added' ? token.colorSuccessBg : line.kind === 'removed' ? token.colorErrorBg : undefined,
            color: line.kind === 'same' ? token.colorTextSecondary : token.colorText,
          }}
        >
          {line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '- ' : '  '}
          {line.text}
        </div>
      ))}
    </div>
  );
}

/** How much of the diff shows before it's expanded - about five lines. */
const FOLDED_DIFF_HEIGHT = 96;

/** A diff kept short - a few lines of it, then a fade and an expand icon at the foot that opens it to
 * its full height (and folds it again). The icon only appears when there's more than fits. */
export function ExpandableDiff({ before, after }: { before: string; after: string }) {
  const token = useTokens();
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = box.current;
    if (element && !expanded) setOverflows(element.scrollHeight > element.clientHeight + 1);
  }, [before, after, expanded]);
  return (
    <Flex vertical gap={0}>
      <div
        ref={box}
        style={{
          position: 'relative',
          maxHeight: expanded ? undefined : FOLDED_DIFF_HEIGHT,
          overflow: 'hidden',
          borderRadius: token.borderRadius,
        }}
      >
        <DiffView before={before} after={after} maxHeight={null} changesOnly />
        {overflows && !expanded && (
          // A fade over the last line, so the cut reads as "there's more".
          <div
            style={{
              position: 'absolute',
              left: 0,
              right: 0,
              bottom: 0,
              height: 32,
              background: `linear-gradient(to bottom, transparent, ${token.colorBgContainer})`,
              pointerEvents: 'none',
            }}
          />
        )}
      </div>
      {overflows && (
        <Button
          type="text"
          size="small"
          icon={expanded ? <UpOutlined /> : <DownOutlined />}
          onClick={() => setExpanded(!expanded)}
          aria-label={expanded ? 'Show less of the changes' : 'Show all the changes'}
          style={{ alignSelf: 'center' }}
        />
      )}
    </Flex>
  );
}
