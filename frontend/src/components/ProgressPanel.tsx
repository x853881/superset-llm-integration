import React, { useEffect, useState } from 'react';
import { Button, Flex, Spin, Typography } from 'antd';
import CheckCircleFilled from '@ant-design/icons/CheckCircleFilled';
import ExclamationCircleFilled from '@ant-design/icons/ExclamationCircleFilled';
import LoadingOutlined from '@ant-design/icons/LoadingOutlined';
import { REASONING_FIELD } from '../progress';
import type { CallProgress, ProgressStage } from '../progress';
import type { CallKind } from '../tabState';
import { useWording } from '../verbosity';
import { SectionLabel, useTokens } from './primitives';
import { StopButton } from './StopButton';
import { StreamingText } from './StreamingText';

/** Ticks while `startedAt` is set, so the elapsed readout below counts up rather than sitting still. */
export function useElapsedMs(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return undefined;
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(handle);
  }, [startedAt]);
  return startedAt === null ? 0 : Math.max(0, now - startedAt);
}

/** The marker beside a step - still to come, running, finished, or finished badly. */
function StageIcon({ status }: { status: ProgressStage['status'] }) {
  const token = useTokens();
  if (status === 'active') return <Spin indicator={<LoadingOutlined spin />} size="small" />;
  if (status === 'failed') {
    return <ExclamationCircleFilled style={{ color: token.colorWarning, fontSize: token.fontSize }} />;
  }
  if (status === 'pending') {
    // A hollow ring: this step is in the plan and hasn't started.
    return (
      <span
        style={{
          display: 'inline-block',
          width: token.fontSize - 2,
          height: token.fontSize - 2,
          borderRadius: '50%',
          border: `1px solid ${token.colorTextQuaternary}`,
        }}
      />
    );
  }
  return <CheckCircleFilled style={{ color: token.colorSuccess, fontSize: token.fontSize }} />;
}

/**
 * Every step of the call: the ones done, with what they found and how long they took; the one
 * running; and the ones still to come, greyed out.
 *
 * The whole plan is shown from the start (the backend sends it before doing any of it) because the
 * useful question while waiting is not only "what is happening" but "how much of this is left".
 * A finished step keeps its detail on screen rather than collapsing, because "12 tables, 88
 * columns: orders, customers, ..." is exactly what's worth knowing afterwards if the answer turns
 * out to be wrong.
 */
function StageList({ stages }: { stages: ProgressStage[] }) {
  const token = useTokens();
  return (
    <Flex vertical gap={token.marginXS}>
      {stages.map(stage => (
        <Flex key={stage.key} gap={token.marginXS} align="flex-start">
          <div style={{ width: token.fontSizeLG, flexShrink: 0, marginTop: 3, textAlign: 'center' }}>
            <StageIcon status={stage.status} />
          </div>
          <Flex vertical gap={0} style={{ minWidth: 0, flex: 1 }}>
            <Typography.Text
              strong={stage.status === 'active'}
              type={stage.status === 'pending' ? 'secondary' : undefined}
              style={stage.status === 'pending' ? { color: token.colorTextQuaternary } : undefined}
            >
              {stage.label}
            </Typography.Text>
            {stage.detail && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: token.fontSizeSM, wordBreak: 'break-word' }}
              >
                {stage.detail}
              </Typography.Text>
            )}
          </Flex>
          {stage.seconds !== null && (
            <Typography.Text
              type="secondary"
              style={{ fontSize: token.fontSizeSM, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
            >
              {stage.seconds.toFixed(1)}s
            </Typography.Text>
          )}
        </Flex>
      ))}
    </Flex>
  );
}

/**
 * A reasoning model's thinking, as it thinks.
 *
 * Shown because it is often the longest part of a call - a local 12B model can reason for a minute
 * and a half before writing a character of the answer - and it is the only thing there is to show
 * while that happens. It folds away on its own once the answer starts arriving, since by then there
 * is something better to look at, and the reader can put it back.
 */
function Reasoning({ text, answerStarted }: { text: string; answerStarted: boolean }) {
  const token = useTokens();
  const [override, setOverride] = useState<boolean | null>(null);
  const shown = override ?? !answerStarted;
  return (
    <Flex vertical gap={token.marginXXS}>
      <Flex align="center" justify="space-between" gap={token.marginXS}>
        <SectionLabel>Thinking</SectionLabel>
        <Button type="link" size="small" style={{ padding: 0, height: 'auto' }} onClick={() => setOverride(!shown)}>
          {shown ? 'Hide' : `Show (${text.length.toLocaleString()} characters)`}
        </Button>
      </Flex>
      {shown && <StreamingText text={text} maxHeight={140} />}
    </Flex>
  );
}

/**
 * How each kind of call presents itself while it runs: what it's working from, and which field of
 * the streamed answer to show as it's written (monospace for the ones that write SQL, prose for the
 * ones that write English). What it is *doing* is the step list's job, not a heading's.
 *
 * The ones that write SQL also carry a second field, shown as its own streaming block once it
 * starts arriving rather than waiting to appear all at once in the result card below: the
 * plain-English summary the model writes straight after the query (see the backend's
 * `nlsql_backend.SQL_SUMMARY_FIELD`), or - for accelerate - why the rewrite should be faster.
 * (From sql-helper.)
 */
const PRESENTATION: Record<
  CallKind,
  {
    detail: string | null;
    /** `detail` cut to a line, for Brief. */
    briefDetail?: string;
    field: string; streamLabel: string;
    code: boolean;
    summaryField?: string;
    summaryLabel?: string;
  }
> = {
  generate: {
    detail: 'Nothing is written into the editor until this lands.',
    briefDetail: 'The editor is untouched until it lands.',
    field: 'sqlText',
    streamLabel: 'SQL so far',
    code: true,
    summaryField: 'sqlDetailedDescription',
    summaryLabel: 'Summary so far',
  },
  update: {
    detail: 'Editing the SQL already in the editor, rather than starting again.',
    briefDetail: "Editing the editor's SQL.",
    field: 'sqlText',
    streamLabel: 'SQL so far',
    code: true,
    summaryField: 'sqlDetailedDescription',
    summaryLabel: 'Summary so far',
  },
  refine: {
    detail: null, // the instruction itself is more use than anything that could be said here
    field: 'sqlText',
    streamLabel: 'SQL so far',
    code: true,
    summaryField: 'sqlDetailedDescription',
    summaryLabel: 'Summary so far',
  },
  describe: {
    detail: 'Reading the SQL currently in the editor.',
    briefDetail: "Reading the editor's SQL.",
    field: 'naturalLanguage',
    streamLabel: 'Description so far',
    code: false,
  },
  fix: {
    detail: null,
    field: 'problemSummary',
    streamLabel: 'What it means',
    code: false,
  },
  accelerate: {
    detail: 'Looking for a faster query that returns the same result. Nothing in the editor changes until you apply it.',
    briefDetail: 'Nothing changes until you apply it.',
    field: 'rewrittenSql',
    streamLabel: 'Faster version so far',
    code: true,
    summaryField: 'explanation',
    summaryLabel: 'Why it should be faster',
  },
  'explain-plan': {
    detail: 'The database plans the query - it is not run.',
    briefDetail: 'Planned, not run.',
    field: 'explanation',
    streamLabel: 'Explanation so far',
    code: false,
  },
  breakdown: {
    detail: 'Reading the SQL currently in the editor, part by part.',
    briefDetail: "Reading the editor's SQL, part by part.",
    field: 'summary',
    streamLabel: 'Overview so far',
    code: false,
  },
  interrogate: {
    detail: 'Reading the structure of every table - names, types, keys, comments; never the data - and having the model describe them.',
    briefDetail: 'Table structure only, never the data.',
    field: 'none',
    streamLabel: '',
    code: false,
  },
};

export interface ProgressPanelProps {
  kind: CallKind;
  /** Overrides what this particular call says it's working from - the refine instruction itself,
   * which is more use than anything `PRESENTATION` could say in general. */
  detail?: string | null;
  progress: CallProgress;
  elapsedMs: number;
  onStop: () => void;
}

/**
 * What is happening while a call is in flight: which stage the pipeline has reached, the answer as
 * the model writes it, and how long it has all been going on for. The editor is left untouched
 * until the call lands, so this is also the reassurance that nothing has been written yet - and
 * the one place the call can be called off from.
 */
export function ProgressPanel({ kind, detail, progress, elapsedMs, onStop }: ProgressPanelProps) {
  const token = useTokens();
  const say = useWording();
  const presentation = PRESENTATION[kind];
  const workingFrom = detail === undefined ? say(presentation.detail, presentation.briefDetail ?? presentation.detail) : detail;
  const streamed = progress.text[presentation.field] ?? '';
  const streamedSummary = presentation.summaryField ? progress.text[presentation.summaryField] ?? '' : '';
  const reasoning = progress.text[REASONING_FIELD] ?? '';
  return (
    <Flex vertical gap={token.marginSM}>
      <Flex align="center" justify="space-between" gap={token.marginXS}>
        <SectionLabel>Progress</SectionLabel>
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {(elapsedMs / 1000).toFixed(1)}s elapsed
        </Typography.Text>
      </Flex>
      {workingFrom && (
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {workingFrom}
        </Typography.Text>
      )}

      {progress.stages.length > 0 ? (
        <StageList stages={progress.stages} />
      ) : (
        <Flex gap={token.marginXS} align="center">
          <Spin indicator={<LoadingOutlined spin />} size="small" />
          <Typography.Text type="secondary">Starting</Typography.Text>
        </Flex>
      )}

      {reasoning !== '' && <Reasoning text={reasoning} answerStarted={streamed !== ''} />}

      {streamed !== '' && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>{presentation.streamLabel}</SectionLabel>
          <StreamingText text={streamed} code={presentation.code} />
        </Flex>
      )}

      {streamedSummary !== '' && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>{presentation.summaryLabel}</SectionLabel>
          <StreamingText text={streamedSummary} />
        </Flex>
      )}

      <StopButton onStop={onStop} what="this request" />
    </Flex>
  );
}
