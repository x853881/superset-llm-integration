import React from 'react';
import { Flex, Typography } from 'antd';
import { useTokens } from './primitives';

export interface EmptyWorkflowProps {
  /** What this workflow would do, said as the reason it has nothing to show yet. */
  children: React.ReactNode;
  /** The way out of it, where there is one - "ask a question first", "run the query". */
  next?: React.ReactNode;
}

/**
 * A workflow with nothing to do yet. Each tab is always there to be clicked, so each one needs an
 * answer to "why is this empty" that isn't a blank column - and, where there is one, the thing to
 * do about it.
 */
export function EmptyWorkflow({ children, next }: EmptyWorkflowProps) {
  const token = useTokens();
  return (
    <Flex vertical gap={token.marginXS} style={{ padding: `${token.paddingLG}px 0` }}>
      <Typography.Text type="secondary">{children}</Typography.Text>
      {next && (
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {next}
        </Typography.Text>
      )}
    </Flex>
  );
}
