import React from 'react';
import { Button, Flex, Input, Radio, Tooltip, Typography } from 'antd';
import { components } from '@apache-superset/core';
import CopyOutlined from '@ant-design/icons/CopyOutlined';
import EditOutlined from '@ant-design/icons/EditOutlined';
import QuestionCircleFilled from '@ant-design/icons/QuestionCircleFilled';
import RedoOutlined from '@ant-design/icons/RedoOutlined';
import ReloadOutlined from '@ant-design/icons/ReloadOutlined';
import UndoOutlined from '@ant-design/icons/UndoOutlined';
import type { SqlGenerationResult } from '../sqlGenerationResult';
import { useWording } from '../verbosity';
import { BulletList, Callout, SectionLabel, useTokens } from './primitives';
import { Prose } from './Prose';
import { ChangeList } from './ChangeList';

/** A left-aligned, wrapping, full-width button - used wherever a suggestion is offered as a card. */
function SuggestionButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
}) {
  const token = useTokens();
  return (
    <Button
      block
      disabled={disabled}
      onClick={onClick}
      style={{
        height: 'auto',
        whiteSpace: 'normal',
        textAlign: 'left',
        justifyContent: 'flex-start',
        padding: `${token.paddingXS}px ${token.paddingSM}px`,
      }}
    >
      {children}
    </Button>
  );
}

export interface SuccessResultProps {
  result: SqlGenerationResult;
  /** A refinement rewrites SQL that was already there; a generation puts a first draft in place. */
  refinement: boolean;
  onSuggestionSelected: (suggestion: string) => void;
  busy: boolean;
}

/**
 * What the SQL now in the editor does, which tables it touched and what it had to assume to get
 * there - the "review it before you run it" half of the panel - followed by the refinements it
 * suggests itself.
 */
export function SuccessResult({ result, refinement, onSuggestionSelected, busy }: SuccessResultProps) {
  const token = useTokens();
  const say = useWording();
  const lineCount = result.sqlText ? result.sqlText.trim().split('\n').length : 0;
  return (
    <Flex vertical gap={token.marginSM}>
      <components.Alert type="success" showIcon closable={false}>
        <Typography.Text strong>{refinement ? 'Query updated.' : 'Draft ready.'}</Typography.Text>{' '}
        {lineCount} {lineCount === 1 ? 'line' : 'lines'}{' '}
        {say('placed in the editor. Read it over, then press Run.', 'in the editor - check, then Run.')}
      </components.Alert>

      {result.sqlDetailedDescription && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>What this query does</SectionLabel>
          <Typography.Text>
            <Prose text={result.sqlDetailedDescription} />
          </Typography.Text>
        </Flex>
      )}

      {/* A refinement's or an update's - SQL written from scratch has none. */}
      <ChangeList changes={result.changes} />

      {result.tablesUsed.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Tables it used</SectionLabel>
          <Flex gap={token.marginXXS} wrap="wrap">
            {result.tablesUsed.map(table => (
              <Typography.Text key={table} code>
                {table}
              </Typography.Text>
            ))}
          </Flex>
        </Flex>
      )}

      {result.assumptionsMade.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Assumptions it made</SectionLabel>
          <BulletList items={result.assumptionsMade} />
        </Flex>
      )}

      {result.suggestionsForRefinement.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Refine it</SectionLabel>
          {result.suggestionsForRefinement.map(suggestion => (
            <SuggestionButton key={suggestion} disabled={busy} onClick={() => onSuggestionSelected(suggestion)}>
              {suggestion}
            </SuggestionButton>
          ))}
        </Flex>
      )}
    </Flex>
  );
}

export interface ClarificationsCardProps {
  result: SqlGenerationResult;
  specificAnswers: Record<number, string>;
  generalAnswers: Record<number, string>;
  onSpecificAnswer: (index: number, value: string) => void;
  onGeneralAnswer: (index: number, value: string) => void;
  canRetry: boolean;
  onRetry: () => void;
  onCancel: () => void;
  busy: boolean;
}

/**
 * Where the question was ambiguous enough that guessing would be worse than asking. Nothing has
 * been written into the editor at this point, and nothing will be until every question here has
 * an answer - see `PendingClarifications` for how the retry is grounded the same way the original
 * call was.
 */
export function ClarificationsCard({
  result,
  specificAnswers,
  generalAnswers,
  onSpecificAnswer,
  onGeneralAnswer,
  canRetry,
  onRetry,
  onCancel,
  busy,
}: ClarificationsCardProps) {
  const token = useTokens();
  const say = useWording();
  return (
    <Callout tone="warning" gap={token.marginSM} style={{ padding: token.paddingSM, borderRadius: token.borderRadiusLG }}>
      <Flex gap={token.marginXS} align="flex-start">
        <QuestionCircleFilled style={{ color: token.colorWarning, marginTop: token.marginXXS }} />
        <Flex vertical gap={token.marginXXS}>
          <Typography.Text strong>Needs an answer from you</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {say('Paused — no SQL has been written yet.', 'No SQL written yet.')}
          </Typography.Text>
        </Flex>
      </Flex>

      {result.specificClarificationsNeeded.map((clarification, index) => (
        <Flex vertical gap={token.marginXXS} key={clarification.question}>
          <Typography.Text>{clarification.question}</Typography.Text>
          <Radio.Group
            value={specificAnswers[index]}
            onChange={event => onSpecificAnswer(index, event.target.value)}
            style={{ display: 'flex', flexDirection: 'column', gap: token.marginXXS }}
          >
            {clarification.options.map(option => (
              <Radio
                key={option}
                value={option}
                style={{
                  display: 'flex',
                  marginInlineEnd: 0,
                  padding: `${token.paddingXS}px ${token.paddingSM}px`,
                  background: specificAnswers[index] === option ? token.colorInfoBg : token.colorBgContainer,
                  border: `1px solid ${
                    specificAnswers[index] === option ? token.colorInfoBorder : token.colorBorderSecondary
                  }`,
                  borderRadius: token.borderRadius,
                  whiteSpace: 'normal',
                  alignItems: 'flex-start',
                }}
              >
                {option}
              </Radio>
            ))}
          </Radio.Group>
        </Flex>
      ))}

      {result.generalClarificationsNeeded.map((question, index) => (
        <Flex vertical gap={token.marginXXS} key={question}>
          <Typography.Text>{question}</Typography.Text>
          <Input.TextArea
            value={generalAnswers[index] ?? ''}
            onChange={event => onGeneralAnswer(index, event.target.value)}
            autoSize={{ minRows: 2, maxRows: 6 }}
          />
        </Flex>
      ))}

      <Flex gap={token.marginXS} wrap="wrap">
        <Button type="primary" disabled={!canRetry || busy} onClick={onRetry}>
          Continue
        </Button>
        <Button disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </Flex>
    </Callout>
  );
}

export interface FailureResultProps {
  result: SqlGenerationResult;
  onRephrase: () => void;
  onRetry: () => void;
  busy: boolean;
}

/**
 * The "it could not be done" shape: what was actually looked at, and what the user could do
 * instead - rather than plausible-looking SQL against columns that do not exist.
 */
export function FailureResult({ result, onRephrase, onRetry, busy }: FailureResultProps) {
  const token = useTokens();
  return (
    <Flex vertical gap={token.marginSM}>
      <components.Alert type="error" showIcon closable={false}>
        <Typography.Text strong>I could not answer this.</Typography.Text>{' '}
        {result.failureMessage || 'No SQL has been written.'}
      </components.Alert>

      {result.whatWasTriedOnFailure.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>What it looked at</SectionLabel>
          <BulletList items={result.whatWasTriedOnFailure} />
        </Flex>
      )}

      {result.suggestionsForFixingOnFailure.length > 0 && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>What you could try</SectionLabel>
          <BulletList items={result.suggestionsForFixingOnFailure} />
        </Flex>
      )}

      <Flex gap={token.marginXS} wrap="wrap">
        <Button icon={<EditOutlined />} disabled={busy} onClick={onRephrase}>
          Rephrase
        </Button>
        <Button icon={<ReloadOutlined />} disabled={busy} onClick={onRetry}>
          Try again
        </Button>
      </Flex>
    </Flex>
  );
}

/** A hand edit of the SQL, which has no generation result of its own but is still a step in the
 * timeline the undo/redo buttons walk. */
export function ManualEditNote() {
  const token = useTokens();
  const say = useWording();
  return (
    <Typography.Text type="secondary" style={{ fontStyle: 'italic' }}>
      {say(
        <>
          You edited the SQL by hand. Undo steps back through this tab&rsquo;s history, including{' '}
          <span style={{ fontFamily: token.fontFamilyCode }}>Ask</span>&rsquo;s own edits.
        </>,
        'Edited by hand. Undo steps back.',
      )}
    </Typography.Text>
  );
}

/** A faster version from Accelerate, applied - a step in the same undo timeline as everything else,
 * so undo puts the original back to compare against. */
export function AccelerateAppliedNote() {
  const say = useWording();
  return (
    <Typography.Text type="secondary" style={{ fontStyle: 'italic' }}>
      {say(
        'A faster version from Accelerate was applied. Undo puts the original query back.',
        'Faster version applied. Undo restores the original.',
      )}
    </Typography.Text>
  );
}

/** A correction applied from the SQL-error workflow (`FixErrorsCard`), which is a step in the same
 * timeline the undo/redo buttons walk - so undo puts the query that was failing back. */
export function FixAppliedNote() {
  const say = useWording();
  return (
    <Typography.Text type="secondary" style={{ fontStyle: 'italic' }}>
      {say(
        'A fix for the editor’s SQL errors was applied. Undo puts back what was there before.',
        'Fix applied. Undo puts the old SQL back.',
      )}
    </Typography.Text>
  );
}

export interface ResultActionsProps {
  onRegenerate: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onCopy: () => void;
  /** Left out entirely for a result that wasn't produced by asking a question - "regenerate" means
   * asking it again, which is the Ask workflow's action, not something to offer under a refinement
   * or an applied fix. */
  showRegenerate: boolean;
  canRegenerate: boolean;
  canUndo: boolean;
  canRedo: boolean;
  canCopy: boolean;
  copied: boolean;
  /** Where the editor currently sits in this tab's edit timeline, as "position of total". */
  position: number;
  total: number;
}

/** What can be done to the SQL now in the editor, without leaving the review step. */
export function ResultActions({
  onRegenerate,
  onUndo,
  onRedo,
  onCopy,
  showRegenerate,
  canRegenerate,
  canUndo,
  canRedo,
  canCopy,
  copied,
  position,
  total,
}: ResultActionsProps) {
  const token = useTokens();
  return (
    <Flex align="center" gap={token.marginXS} wrap="wrap">
      {showRegenerate && (
        <Button size="small" icon={<ReloadOutlined />} disabled={!canRegenerate} onClick={onRegenerate}>
          Regenerate
        </Button>
      )}
      <Tooltip title="Step back through this tab's edits">
        <Button size="small" icon={<UndoOutlined />} disabled={!canUndo} onClick={onUndo}>
          Undo
        </Button>
      </Tooltip>
      <Tooltip title="Step forward again">
        <Button size="small" icon={<RedoOutlined />} disabled={!canRedo} onClick={onRedo}>
          Redo
        </Button>
      </Tooltip>
      <Button size="small" icon={<CopyOutlined />} disabled={!canCopy} onClick={onCopy}>
        {copied ? 'Copied' : 'Copy'}
      </Button>
      {total > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {position} of {total}
        </Typography.Text>
      )}
    </Flex>
  );
}
