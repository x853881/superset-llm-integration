import React from 'react';
import { Flex, Typography } from 'antd';
import type { GenerationDisplay } from '../tabState';
import { FeedbackBar } from './FeedbackBar';
import {
  AccelerateAppliedNote,
  ClarificationsCard,
  FailureResult,
  FixAppliedNote,
  ManualEditNote,
  ResultActions,
  SuccessResult,
} from './ResultSections';
import { SectionLabel, useTokens } from './primitives';

export interface GenerationResultProps {
  display: GenerationDisplay;
  specificAnswers: Record<number, string>;
  generalAnswers: Record<number, string>;
  onSpecificAnswer: (index: number, value: string) => void;
  onGeneralAnswer: (index: number, value: string) => void;
  canRetry: boolean;
  onRetryWithClarifications: () => void;
  onCancelClarifications: () => void;
  onSuggestionSelected: (suggestion: string) => void;
  onRephrase: () => void;
  onRegenerate: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onCopy: () => void;
  copied: boolean;
  canRegenerate: boolean;
  canUndo: boolean;
  canRedo: boolean;
  canCopy: boolean;
  position: number;
  total: number;
  busy: boolean;
}

/**
 * Renders whichever of `SqlGenerationResult`'s three shapes `display` carries - or, for the two
 * kinds of step that have no result of their own, the note standing in for it: a hand edit, or a
 * correction applied from the SQL-error workflow. The result actions below are the
 * ones that act on SQL now sitting in the editor, so they're shown for the two shapes that put
 * something there and left out of the two that deliberately didn't (`ClarificationsRequired` and
 * `Failure`), which offer their own way forward instead.
 */
export function GenerationResult({
  display,
  specificAnswers,
  generalAnswers,
  onSpecificAnswer,
  onGeneralAnswer,
  canRetry,
  onRetryWithClarifications,
  onCancelClarifications,
  onSuggestionSelected,
  onRephrase,
  onRegenerate,
  onUndo,
  onRedo,
  onCopy,
  copied,
  canRegenerate,
  canUndo,
  canRedo,
  canCopy,
  position,
  total,
  busy,
}: GenerationResultProps) {
  const token = useTokens();
  const { result } = display;

  if (result && result.status === 'ClarificationsRequired') {
    return (
      <ClarificationsCard
        result={result}
        specificAnswers={specificAnswers}
        generalAnswers={generalAnswers}
        onSpecificAnswer={onSpecificAnswer}
        onGeneralAnswer={onGeneralAnswer}
        canRetry={canRetry}
        onRetry={onRetryWithClarifications}
        onCancel={onCancelClarifications}
        busy={busy}
      />
    );
  }

  if (result && result.status === 'Failure') {
    return (
      <Flex vertical gap={token.marginXS}>
        <FailureResult result={result} onRephrase={onRephrase} onRetry={onRegenerate} busy={busy} />
        <FeedbackBar trace={result.trace} target="sql-failure" />
      </Flex>
    );
  }

  return (
    <Flex vertical gap={token.marginSM}>
      {display.type === 'refine' && display.command && (
        <Flex vertical gap={token.marginXXS}>
          <SectionLabel>Refinement applied</SectionLabel>
          <Typography.Text>
            {'“'}
            {display.command}
            {'”'}
          </Typography.Text>
        </Flex>
      )}
      {result ? (
        <SuccessResult
          result={result}
          refinement={display.type === 'refine'}
          onSuggestionSelected={onSuggestionSelected}
          busy={busy}
        />
      ) : display.type === 'fix' ? (
        <FixAppliedNote />
      ) : display.type === 'accelerate' ? (
        <AccelerateAppliedNote />
      ) : (
        <ManualEditNote />
      )}
      <ResultActions
        showRegenerate={display.type === 'generate'}
        onRegenerate={onRegenerate}
        onUndo={onUndo}
        onRedo={onRedo}
        onCopy={onCopy}
        canRegenerate={canRegenerate}
        canUndo={canUndo}
        canRedo={canRedo}
        canCopy={canCopy}
        copied={copied}
        position={position}
        total={total}
      />
      {result && <FeedbackBar trace={result.trace} target="sql" />}
    </Flex>
  );
}
