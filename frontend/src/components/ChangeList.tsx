import React from 'react';
import { Flex, Typography } from 'antd';
import type { RewriteChange } from '../performanceResult';
import { SectionLabel, useTokens } from './primitives';
import { Prose } from './Prose';

/** What a refinement, an update, a fix or an acceleration changed, each with why: "summary - why".
 * The same changes are notes beside the lines they're about in "Compare with original". */
export function ChangeList({ changes, title = 'What changed' }: { changes: RewriteChange[] | undefined; title?: string }) {
  const token = useTokens();
  if (!changes || changes.length === 0) return null;
  return (
    <Flex vertical gap={token.marginXXS}>
      <SectionLabel>{title}</SectionLabel>
      <ul style={{ margin: 0, paddingLeft: token.paddingMD, lineHeight: token.lineHeight }}>
        {changes.map((change, index) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={index} style={{ marginBottom: token.marginXXS }}>
            <Prose text={change.summary} />
            {change.why && (
              <Typography.Text type="secondary">
                {change.summary ? ' - ' : ''}
                <Prose text={change.why} />
              </Typography.Text>
            )}
          </li>
        ))}
      </ul>
    </Flex>
  );
}
