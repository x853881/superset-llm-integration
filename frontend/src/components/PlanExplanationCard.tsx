import React from 'react';
import { Collapse, Flex, Typography } from 'antd';
import type { PlanExplanationResult } from '../performanceResult';
import { useWording } from '../verbosity';
import { FeedbackBar } from './FeedbackBar';
import { PlanOutline } from './AccelerateCard';
import { SectionLabel, useTokens } from './primitives';
import { Freshness, StaleNotice } from './StaleNotice';
import { Prose } from './Prose';

export interface PlanExplanationCardProps {
  result: PlanExplanationResult;
  /** Whether the editor's SQL is still the SQL that was planned (see `StaleNotice`). */
  freshness: Freshness;
}

/** The database's plan for the query, read back in plain English - with the plan itself underneath
 * for anyone who wants to check the reading. */
export function PlanExplanationCard({ result, freshness }: PlanExplanationCardProps) {
  const token = useTokens();
  const say = useWording();
  const { plan } = result;
  const cost = plan.totalCost === null ? '' : `${plan.totalCost.toLocaleString(undefined, { maximumFractionDigits: 0 })} (${plan.costUnit})`;
  return (
    <Flex vertical gap={token.marginSM}>
      <SectionLabel>How the database would run it</SectionLabel>
      <StaleNotice freshness={freshness} what="plan" />
      {plan.totalCost !== null && (
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {say(
            `Estimated cost ${cost}${plan.steps !== null ? ` across ${plan.steps} steps` : ''}. The query was planned, not run.`,
            `Cost ${cost}${plan.steps !== null ? `, ${plan.steps} steps` : ''} - planned, not run.`,
          )}
        </Typography.Text>
      )}
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
        <Prose text={result.explanation} />
      </Typography.Paragraph>
      <Collapse
        size="small"
        items={[{ key: 'plan', label: `The plan${plan.warnings.length ? ` (${plan.warnings.length} flagged)` : ''}`, children: <PlanOutline plan={plan} /> }]}
      />
      <FeedbackBar trace={result.trace} target="explain-plan" />
    </Flex>
  );
}
