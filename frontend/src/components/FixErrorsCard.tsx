import React from 'react';
import { Button, Flex, Tag, Typography } from 'antd';
import { components } from '@apache-superset/core';
import CloseOutlined from '@ant-design/icons/CloseOutlined';
import ToolOutlined from '@ant-design/icons/ToolOutlined';
import type { CallProgress } from '../progress';
import type { SqlError, SqlFixResult } from '../sqlFixResult';
import { useWording } from '../verbosity';
import { FeedbackBar } from './FeedbackBar';
import { BulletList, Callout, SectionLabel, ToneIcon, useTokens } from './primitives';
import { ProgressPanel } from './ProgressPanel';
import { StreamingText } from './StreamingText';
import { Prose } from './Prose';

/** How many of the errors to list before summarising the rest - a query with twenty complaints
 * against it doesn't need all twenty to make the point, and the panel is 400px wide. */
const MAX_LISTED_ERRORS = 4;

function ErrorList({ errors }: { errors: SqlError[] }) {
  const token = useTokens();
  const listed = errors.slice(0, MAX_LISTED_ERRORS);
  return (
    <Flex vertical gap={token.marginXXS}>
      {listed.map((error, index) => (
        <Flex key={`${error.line ?? 'x'}-${index}`} gap={token.marginXXS} align="flex-start">
          {error.line !== undefined && (
            <Typography.Text
              type="secondary"
              style={{ fontFamily: token.fontFamilyCode, fontSize: token.fontSizeSM, flexShrink: 0 }}
            >
              line {error.line}
            </Typography.Text>
          )}
          <Typography.Text style={{ fontSize: token.fontSizeSM, wordBreak: 'break-word' }}>
            {error.message}
          </Typography.Text>
        </Flex>
      ))}
      {errors.length > listed.length && (
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          and {errors.length - listed.length} more
        </Typography.Text>
      )}
    </Flex>
  );
}

/** The model's own confidence in a correction, said plainly rather than left to be inferred from
 * the fact that something was offered at all. */
function ConfidenceTag({ confidence }: { confidence: string }) {
  const normalised = confidence.trim().toLowerCase();
  if (!normalised) return null;
  const color = normalised === 'high' ? 'green' : normalised === 'low' ? 'orange' : 'default';
  return <Tag color={color}>{confidence} confidence</Tag>;
}

export interface FixErrorsCardProps {
  /** What the editor is complaining about - the validator's gutter annotations, or the error a run
   * came back with. */
  errors: SqlError[];
  /** Set while the explanation is being generated. */
  progress: CallProgress | null;
  elapsedMs: number;
  result: SqlFixResult | null;
  /** True when one of the other workflows has a call of its own in flight. Doesn't block anything
   * here - the tabs are separate workflows and each has its own Stop - but it is worth saying, since
   * against a local model two calls at once means both of them crawl. */
  otherRunning: boolean;
  onExplain: () => void;
  onStop: () => void;
  /** Replaces the editor's contents with the corrected query. */
  onApply: (sql: string) => void;
  onDismiss: () => void;
}

/**
 * The SQL-error workflow: what the editor is complaining about, what that actually means, and the
 * query as it should have been written.
 *
 * Its own card, above the ask/refine flow rather than inside it, because it's a different job with
 * a different starting point - the query is already there and something outside this panel has
 * objected to it. Nothing here happens on its own: the errors are shown as soon as they appear, but
 * explaining them costs a model call, and replacing what's in the editor is a separate decision
 * again (`onApply`), taken after reading what the change would be.
 */
export function FixErrorsCard({
  errors,
  progress,
  elapsedMs,
  result,
  otherRunning,
  onExplain,
  onStop,
  onApply,
  onDismiss,
}: FixErrorsCardProps) {
  const token = useTokens();
  const say = useWording();
  const sources = new Set(errors.map(error => error.source));
  const heading = errors.length === 1 ? '1 problem in this query' : `${errors.length} problems in this query`;

  return (
    // Marked in the error colour (border, bar, mark and heading) rather than filled with it: the card
    // carries a long explanation to read, and a page of prose on a red wash is hard work.
    <Callout tone="error" tinted={false} gap={token.marginSM} style={{ padding: token.paddingSM, borderRadius: token.borderRadiusLG }}>
      <Flex align="flex-start" justify="space-between" gap={token.marginXS}>
        <Flex vertical gap={token.marginXXS} style={{ minWidth: 0 }}>
          <Flex align="center" gap={token.marginXS}>
            <ToneIcon tone="error" />
            <Typography.Text strong style={{ color: token.colorErrorText }}>
              {heading}
            </Typography.Text>
          </Flex>
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {sources.has('query')
              ? say('Reported by the database when the query last ran.', "From the database's last run.")
              : sources.has('parser')
                ? say(
                    'Found by parsing the SQL — this database has no SQL validator in Superset, so only syntax is checked until it runs.',
                    'Syntax only - no validator for this database.',
                  )
                : say("Reported by Superset's SQL validator — the same errors marked in the editor's gutter.", "From Superset's SQL validator.")}
          </Typography.Text>
        </Flex>
        <Button
          type="text"
          size="small"
          icon={<CloseOutlined />}
          onClick={onDismiss}
          aria-label="Dismiss these problems"
        />
      </Flex>

      <ErrorList errors={errors} />

      {progress && (
        <ProgressPanel kind="fix" progress={progress} elapsedMs={elapsedMs} onStop={onStop} />
      )}

      {!progress && !result && (
        <>
          <Button type="primary" icon={<ToolOutlined />} onClick={onExplain}>
            Explain and fix
          </Button>
          {otherRunning && (
            <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
              {say('Another workflow is still running. This will share the model with it.', 'Shares the model with a running workflow.')}
            </Typography.Text>
          )}
        </>
      )}

      {!progress && result && <FixResult result={result} onApply={onApply} onRetry={onExplain} />}
      {!progress && result && <FeedbackBar trace={result.trace} target="fix" />}
    </Callout>
  );
}

function FixResult({
  result,
  onApply,
  onRetry,
}: {
  result: SqlFixResult;
  onApply: (sql: string) => void;
  onRetry: () => void;
}) {
  const token = useTokens();

  if (result.status === 'Failure') {
    return (
      <Flex vertical gap={token.marginSM}>
        <components.Alert type="warning" showIcon closable={false}>
          {result.failureMessage || "The error couldn't be explained."}
        </components.Alert>
        <Button onClick={onRetry}>Try again</Button>
      </Flex>
    );
  }

  return (
    <Flex vertical gap={token.marginSM}>
      {result.problemSummary && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>What it means</SectionLabel>
          <Typography.Text>
            <Prose text={result.problemSummary} />
          </Typography.Text>
        </Flex>
      )}

      {result.cause && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Why this query hit it</SectionLabel>
          <Typography.Text>
            <Prose text={result.cause} />
          </Typography.Text>
        </Flex>
      )}

      {result.fixExplanation && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>{result.status === 'Fixed' ? 'The fix' : 'What would be needed'}</SectionLabel>
          <Typography.Text>
            <Prose text={result.fixExplanation} />
          </Typography.Text>
        </Flex>
      )}

      {result.remainingRisks.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Still worth checking</SectionLabel>
          <BulletList items={result.remainingRisks} />
        </Flex>
      )}

      {result.status === 'Fixed' && result.fixedSql && (
        <>
          <Flex vertical gap={token.marginXXS}>
            <Flex align="center" justify="space-between" gap={token.marginXS}>
              <SectionLabel>Corrected query</SectionLabel>
              <ConfidenceTag confidence={result.confidence} />
            </Flex>
            <StreamingText text={result.fixedSql} code />
          </Flex>
          <Flex gap={token.marginXS} wrap="wrap">
            <Button type="primary" onClick={() => onApply(result.fixedSql)}>
              Replace the SQL in the editor
            </Button>
            <Button onClick={onRetry}>Explain again</Button>
          </Flex>
        </>
      )}

      {result.status === 'Explained' && <Button onClick={onRetry}>Try again</Button>}
    </Flex>
  );
}
