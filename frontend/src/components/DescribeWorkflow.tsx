import React from 'react';
import { Button, Flex, Typography } from 'antd';
import ArrowRightOutlined from '@ant-design/icons/ArrowRightOutlined';
import PartitionOutlined from '@ant-design/icons/PartitionOutlined';
import type { BreakdownSection } from '../performanceResult';
import type { TraceRef } from '../progress';
import type { BreakdownEntry } from '../tabState';
import { useWording } from '../verbosity';
import { FeedbackBar } from './FeedbackBar';
import { SectionLabel, useTokens } from './primitives';
import { SqlBreakdown } from './SqlBreakdown';
import { Freshness, Refreshable, StaleNotice } from './StaleNotice';
import { StreamingText } from './StreamingText';

export interface DescribeWorkflowProps {
  /** "Describe the editor's SQL" cold, "Update the question from the SQL" once there's a question
   * this would be an edit of. */
  actionLabel: string;
  actionDisabled: boolean;
  onDescribe: () => void;
  /** The last description this workflow produced, and the SQL it was produced from. */
  description: { text: string; sql: string; trace?: TraceRef } | null;
  /** Whether that description still describes the editor's SQL, or is being rewritten. */
  freshness: Freshness;
  onOpenInAsk: () => void;
  busy: boolean;
  /** The section-by-section description: asking for it, and what came back. */
  onBreakdown: () => void;
  breakdownDisabled: boolean;
  breakdown: BreakdownEntry | null;
  breakdownFreshness: Freshness;
  breakdownChangedLines?: number | null;
  onSelectInEditor: ((section: BreakdownSection) => void) | null;
  /** The progress display for a call running on this tab, placed between the actions and the results. */
  progress?: React.ReactNode;
}

/**
 * The reverse direction: SQL in the editor, read back as the question it answers.
 *
 * Its own workflow rather than a link inside the ask composer, because it starts from the opposite
 * end - there's already SQL, and what's missing is the sentence describing it. What it produces is
 * the ask workflow's input, so the way out of here is into that tab with the description already in
 * the box.
 */
export function DescribeWorkflow({
  actionLabel,
  actionDisabled,
  onDescribe,
  description,
  freshness,
  onOpenInAsk,
  busy,
  onBreakdown,
  breakdownDisabled,
  breakdown,
  breakdownFreshness,
  breakdownChangedLines,
  onSelectInEditor,
  progress,
}: DescribeWorkflowProps) {
  const token = useTokens();
  const say = useWording();
  return (
    <Flex vertical gap={token.marginSM}>
      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
        {say(
          'Reads the SQL currently in the editor and says what it does - as the question it answers, or section by section (also in the “Break it down” tab under the editor, beside the SQL). Nothing in the editor is changed.',
          "Says what the editor's SQL does, without changing it.",
        )}
      </Typography.Text>

      <Flex gap={token.marginXS} wrap="wrap">
        <Button type="primary" disabled={actionDisabled} onClick={onDescribe}>
          {actionLabel}
        </Button>
        <Button icon={<PartitionOutlined />} disabled={breakdownDisabled} onClick={onBreakdown}>
          Break it down
        </Button>
      </Flex>

      {progress}

      {description && (
        <Refreshable refreshing={freshness === 'refreshing'}>
        <Flex vertical gap={token.marginXS}>
          <Flex align="center" justify="space-between" gap={token.marginXS} wrap="wrap">
            <SectionLabel>Description</SectionLabel>
            <StaleNotice freshness={freshness} what="description" />
          </Flex>
          <StreamingText text={description.text} />
          <Flex gap={token.marginXS} wrap="wrap">
            <Button size="small" icon={<ArrowRightOutlined />} disabled={busy} onClick={onOpenInAsk}>
              Open in Ask
            </Button>
          </Flex>
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {say(
              'Puts it in the question box, grounded in this SQL — so editing it there asks for a change to this query rather than starting a new one.',
              'Edit it there to change this query.',
            )}
          </Typography.Text>
          <FeedbackBar trace={description.trace} target="describe" />
        </Flex>
        </Refreshable>
      )}

      {breakdown && (
        <SqlBreakdown
          result={breakdown.result}
          sql={breakdown.result.sql ?? breakdown.sql}
          freshness={breakdownFreshness}
          changedLines={breakdownChangedLines}
          onSelectInEditor={onSelectInEditor}
        />
      )}
      {breakdown && <FeedbackBar trace={breakdown.result.trace} target="breakdown" />}
    </Flex>
  );
}
