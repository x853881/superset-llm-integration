import React, { useState } from 'react';
import { Button, Collapse, Flex, Tag, Tooltip, Typography } from 'antd';
import { components } from '@apache-superset/core';
import CheckOutlined from '@ant-design/icons/CheckOutlined';
import CopyOutlined from '@ant-design/icons/CopyOutlined';
import ExportOutlined from '@ant-design/icons/ExportOutlined';
import type { AccelerateResult, PlanSummary, RewriteVerdict } from '../performanceResult';
import { useWording } from '../verbosity';
import { FeedbackBar } from './FeedbackBar';
import { BulletList, Callout, Flag, SectionLabel, useTokens } from './primitives';
import { Freshness, StaleNotice } from './StaleNotice';
import { ResultActions, type ResultActionsProps } from './ResultSections';
import { DiffView, ExpandableDiff } from './ExpandableDiff';
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

/** The faster SQL a result offers to put in the editor: a rewrite that differs from the original
 * and passed its checks - null when there's none. */
export function fasterSqlOf(result: AccelerateResult): string | null {
  const { verdict } = result.check;
  return result.rewrittenSql.trim() !== '' && verdict !== 'Unchanged' && verdict !== 'Invalid' ? result.rewrittenSql : null;
}

/**
 * Alpha's Accelerate result, read the same way as Ask's: the faster version has already been put in
 * the editor (`fasterSqlOf`, as a step in the undo timeline), so this shows what it changed (a diff,
 * a few lines high until expanded), says so and offers undo, redo, copy and feedback. With none, it
 * says so instead. Why it should be faster is the summary in the progress above it.
 *
 * The exception is a rewrite the checks say may return different results (MayDiffer): that isn't
 * put in the editor by itself, so this shows what the checks found and an Apply button - and keeps
 * the findings on show once it's applied.
 */
export function AcceleratedResult({
  result,
  originalSql,
  applied,
  onApply,
  busy,
  actions,
}: {
  result: AccelerateResult;
  /** The SQL the faster version was written from - the diff's before. */
  originalSql: string;
  /** The faster version is what's in the editor now. */
  applied: boolean;
  /** Puts the faster version in the editor - offered only when the checks found it may differ. */
  onApply: () => void;
  busy: boolean;
  actions: Omit<ResultActionsProps, 'showRegenerate' | 'onRegenerate' | 'canRegenerate' | 'showPosition'>;
}) {
  const token = useTokens();
  const say = useWording();
  const faster = fasterSqlOf(result);
  const lineCount = faster ? faster.trim().split('\n').length : 0;
  const mayDiffer = faster !== null && result.check.verdict === 'MayDiffer';
  const findings = result.check.findings.map(finding => finding.message);
  return (
    <Flex vertical gap={token.marginSM}>
      {/* Straight under the summary: what it changed, a few lines of it until expanded. */}
      {faster && <ExpandableDiff before={originalSql} after={faster} />}
      {mayDiffer && (
        <Callout tone="warning" gap={token.marginXXS}>
          <SectionLabel>{VERDICTS.MayDiffer.label}</SectionLabel>
          {findings.length > 0 ? (
            <BulletList items={findings} />
          ) : (
            <Typography.Text>{VERDICTS.MayDiffer.hint}</Typography.Text>
          )}
        </Callout>
      )}
      {mayDiffer && !applied ? (
        <Flex align="center" gap={token.marginXS} wrap="wrap">
          <Button type="primary" size="small" icon={<CheckOutlined />} disabled={busy} onClick={onApply}>
            Apply anyway
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {say('The editor is unchanged until you apply it.', 'Editor unchanged until applied.')}
          </Typography.Text>
        </Flex>
      ) : faster ? (
        <components.Alert type="success" showIcon closable={false}>
          <Typography.Text strong>Query updated.</Typography.Text> {lineCount} {lineCount === 1 ? 'line' : 'lines'}{' '}
          {say('placed in the editor. Read it over, then press Run.', 'in the editor - check, then Run.')}
        </components.Alert>
      ) : (
        <components.Alert type="info" showIcon closable={false}>
          {result.check.verdict === 'Invalid'
            ? 'No faster version passed its checks - the editor is unchanged.'
            : 'No faster version found - the editor is unchanged.'}
        </components.Alert>
      )}
      <ResultActions showRegenerate={false} onRegenerate={() => undefined} canRegenerate={false} showPosition={false} {...actions} />
      <FeedbackBar trace={result.trace} target="accelerate" />
    </Flex>
  );
}
