import React, { useMemo, useState } from 'react';
import { Button, Collapse, Flex, Tag, Tooltip, Typography } from 'antd';
import CheckOutlined from '@ant-design/icons/CheckOutlined';
import CopyOutlined from '@ant-design/icons/CopyOutlined';
import ExportOutlined from '@ant-design/icons/ExportOutlined';
import type { AccelerateResult, PlanSummary, RewriteVerdict } from '../performanceResult';
import { lineDiff } from '../lineDiff';
import { useWording } from '../verbosity';
import { FeedbackBar } from './FeedbackBar';
import { BulletList, Callout, Flag, SectionLabel, useTokens } from './primitives';
import { Freshness, StaleNotice } from './StaleNotice';
import { Prose } from './Prose';
import { ChangeList } from './ChangeList';

const VERDICTS: Record<RewriteVerdict, { color: string; label: string; hint: string; briefHint: string }> = {
  Unverified: {
    color: 'blue',
    label: 'No difference found',
    hint: 'The checks found nothing that would change what it returns - which is not proof that nothing does.',
    briefHint: 'The checks found no difference - not proof.',
  },
  MayDiffer: {
    color: 'orange',
    label: 'May return different results',
    hint: 'Something the checks can see would change what the query returns. Read the findings below.',
    briefHint: 'The checks found a difference - see below.',
  },
  Invalid: {
    color: 'red',
    label: 'Not valid',
    hint: 'The rewrite failed its checks even after a second attempt.',
    briefHint: 'Failed its checks twice.',
  },
  Unchanged: {
    color: 'default',
    label: 'No faster version',
    hint: 'The suggestion is the query as it was.',
    briefHint: 'Same as the original.',
  },
};

/** "1,000 → 250 (75% lower)" - the planner's own estimate, so only ever comparable within one database. */
export function costChange(before: number | null, after: number | null): string | null {
  if (before === null || after === null) return null;
  const fmt = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: value >= 100 ? 0 : 1 });
  if (before <= 0) return `${fmt(before)} → ${fmt(after)}`;
  const change = ((after - before) / before) * 100;
  const direction = change <= 0 ? `${fmt(-change)}% lower` : `${fmt(change)}% higher`;
  return `${fmt(before)} → ${fmt(after)} (${direction})`;
}

/** A plan's outline, monospace, scrolled - what the model was shown. */
export function PlanOutline({ plan }: { plan: PlanSummary }) {
  const token = useTokens();
  return (
    <Flex vertical gap={token.marginXS}>
      {plan.warnings.map((warning, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <Flag key={index} tone="warning">
          {warning.message}
        </Flag>
      ))}
      <pre
        style={{
          margin: 0,
          maxHeight: 240,
          overflow: 'auto',
          fontFamily: token.fontFamilyCode,
          fontSize: token.fontSizeSM - 1,
          lineHeight: 1.45,
          background: token.colorFillQuaternary,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: token.borderRadius,
          padding: token.paddingXS,
          whiteSpace: 'pre',
        }}
      >
        {plan.outline}
      </pre>
    </Flex>
  );
}

function DiffView({ before, after }: { before: string; after: string }) {
  const token = useTokens();
  const diff = useMemo(() => lineDiff(before, after), [before, after]);
  if (!diff) return <Typography.Text type="secondary">Too long to show a diff here.</Typography.Text>;
  return (
    <div
      style={{
        maxHeight: 280,
        overflow: 'auto',
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

export interface AccelerateCardProps {
  result: AccelerateResult;
  /** The SQL the rewrite was suggested for. */
  originalSql: string;
  /** Whether the editor's SQL is still the SQL it was suggested for (see `StaleNotice`). */
  freshness: Freshness;
  onApply: () => void;
  onOpenInNewTab: () => void;
  busy: boolean;
}

/**
 * A suggested faster query: how much faster the planner thinks it is, why, what was changed, what
 * the backend's checks found (see `sql_rewrite_check.py`), and the change itself as a diff. After
 * waxwing-sql's `RewriteCard`.
 *
 * Nothing is written into the editor until "Apply" - and applying is a step in the undo timeline
 * like any other - or it can go into a new SQL Lab tab instead, leaving this one as it was, which is
 * how waxwing-sql always does it (so the original stays to compare against).
 */
export function AccelerateCard({ result, originalSql, freshness, onApply, onOpenInNewTab, busy }: AccelerateCardProps) {
  const token = useTokens();
  const say = useWording();
  const [copied, setCopied] = useState(false);
  const verdict = VERDICTS[result.check.verdict] ?? VERDICTS.Unverified;
  const hasRewrite = result.rewrittenSql.trim() !== '' && result.check.verdict !== 'Unchanged';
  const cost = costChange(result.check.originalCost, result.check.rewrittenCost);
  const findingsTone = result.check.verdict === 'Invalid' ? 'error' : result.check.verdict === 'MayDiffer' ? 'warning' : null;

  const copy = () =>
    navigator.clipboard?.writeText(result.rewrittenSql).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });

  const panels = [];
  if (hasRewrite) {
    panels.push({ key: 'diff', label: 'What changed', children: <DiffView before={originalSql} after={result.rewrittenSql} /> });
  }
  if (result.plan) {
    panels.push({ key: 'plan', label: 'The plan the model saw', children: <PlanOutline plan={result.plan} /> });
  }
  if (result.rewrittenPlan) {
    panels.push({ key: 'rewrittenPlan', label: "The rewrite's plan", children: <PlanOutline plan={result.rewrittenPlan} /> });
  }

  return (
    <Flex vertical gap={token.marginSM}>
      <Flex align="center" justify="space-between" gap={token.marginXS} wrap="wrap">
        <SectionLabel>Faster version</SectionLabel>
        <Tooltip title={say(verdict.hint, verdict.briefHint)}>
          <Tag color={verdict.color} style={{ marginInlineEnd: 0 }}>
            {verdict.label}
          </Tag>
        </Tooltip>
      </Flex>

      <StaleNotice freshness={freshness} what="faster version" />

      {cost && (
        <Typography.Text>
          Estimated cost {cost}
          {result.check.costUnit ? (
            <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
              {' '}
              ({result.check.costUnit})
            </Typography.Text>
          ) : null}
        </Typography.Text>
      )}

      {result.explanation && (
        <Typography.Text>
          <Prose text={result.explanation} />
        </Typography.Text>
      )}

      <ChangeList changes={result.changes} title="Changes" />

      {result.check.findings.length > 0 &&
        (findingsTone ? (
          // The checks say the rewrite may not be the same query - the thing to read before applying it.
          <Callout tone={findingsTone} gap={token.marginXXS}>
            <SectionLabel>What the checks found</SectionLabel>
            <BulletList items={result.check.findings.map(finding => finding.message)} />
          </Callout>
        ) : (
          <Flex vertical gap={token.marginXXS}>
            <SectionLabel>What the checks found</SectionLabel>
            <BulletList items={result.check.findings.map(finding => finding.message)} />
          </Flex>
        ))}

      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
        {result.basedOn === 'plan'
          ? say("Based on the database's plan for the query, as well as the SQL.", 'From the plan and the SQL.')
          : result.runPlan
            ? `${say("Based on the SQL alone - the database couldn't plan it", "From the SQL alone - couldn't plan it")}${
                result.planError ? `: ${result.planError}` : '.'
              }`
            : say('Based on the SQL alone ("Run query plan first" was off).', 'From the SQL alone (plan off).')}
      </Typography.Text>

      {panels.length > 0 && <Collapse size="small" items={panels} defaultActiveKey={hasRewrite ? ['diff'] : []} />}

      {hasRewrite && (
        <Flex gap={token.marginXS} wrap="wrap">
          <Button type="primary" size="small" icon={<CheckOutlined />} disabled={busy} onClick={onApply}>
            Apply in this tab
          </Button>
          <Button size="small" icon={<ExportOutlined />} disabled={busy} onClick={onOpenInNewTab}>
            Open in a new tab
          </Button>
          <Button size="small" icon={<CopyOutlined />} onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </Flex>
      )}
      <FeedbackBar trace={result.trace} target="accelerate" />
    </Flex>
  );
}
