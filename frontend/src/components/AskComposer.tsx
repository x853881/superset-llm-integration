import React from 'react';
import { Button, Flex, Input, Typography } from 'antd';
import ArrowLeftOutlined from '@ant-design/icons/ArrowLeftOutlined';
import { useWording } from '../verbosity';
import { isSubmitShortcut, SectionLabel, SparkleIcon, submitShortcutLabel, useTokens } from './primitives';

/** Illustrative questions, offered only to show the shape of a good one on an empty panel. */
const EXAMPLE_QUESTIONS = [
  'Top 10 customers by total order value, all time',
  'Monthly sales by product line for last year',
  'Which regions have the largest average deal size?',
];

export interface AskComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  /** "Generate SQL" cold, "Update SQL" once there's already generated SQL to edit instead. */
  submitLabel: string;
  submitting: boolean;
  submitDisabled: boolean;
  /** Only set when the composer was opened over an existing result ("Edit question"), so there's
   * something to go back to if the question turns out not to need changing after all. */
  onCancelEdit: (() => void) | null;
  /** The example questions under an empty box - off once a conversation is under way. */
  showExamples?: boolean;
  /** Enter sends (Shift+Enter for a new line) rather than the ⌘/Ctrl+Enter chord - Alpha's Ask tab,
   * where a prompt is usually a line. */
  submitOnEnter?: boolean;
  /** Shown at the left of the row with the submit button (Alpha's "Explain query"), where the
   * character count would be. */
  footerStart?: React.ReactNode;
  /** The character count beside the button. */
  showLength?: boolean;
}

/**
 * The ask step: a plain-English question, and what to do with it. Filling the box in from the
 * editor's existing SQL is the Describe workflow's job and lives on its own tab.
 *
 * Enter inserts a newline here by default (questions run to several lines often enough that losing
 * one to a stray Enter is worse than the extra chord), unless `submitOnEnter`; the shortcut to
 * submit is advertised under the button.
 */
export function AskComposer({
  value,
  onChange,
  onSubmit,
  submitLabel,
  submitting,
  submitDisabled,
  onCancelEdit,
  showExamples = true,
  submitOnEnter = false,
  footerStart,
  showLength = true,
}: AskComposerProps) {
  const token = useTokens();
  const say = useWording();
  return (
    <Flex vertical gap={token.marginSM}>
      {onCancelEdit && (
        <Button
          type="link"
          size="small"
          icon={<ArrowLeftOutlined />}
          onClick={onCancelEdit}
          style={{ alignSelf: 'flex-start', padding: 0, height: 'auto' }}
        >
          Back to the last result
        </Button>
      )}
      <Input.TextArea
        value={value}
        onChange={event => onChange(event.target.value)}
        onKeyDown={event => {
          const enterSends = submitOnEnter && event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing;
          if (enterSends || isSubmitShortcut(event)) {
            event.preventDefault();
            onSubmit();
          }
        }}
        autoSize={{ minRows: 5, maxRows: 16 }}
        placeholder={say(
          'Ask a question about this data in plain English, e.g. which product lines brought in the most sales last quarter?',
          'Ask a question in plain English',
        )}
      />
      <Flex align="center" justify="space-between" gap={token.marginXS} wrap="wrap">
        {showLength ? (
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {value.length} characters
          </Typography.Text>
        ) : (
          footerStart ?? <span />
        )}
        <Button
          type="primary"
          icon={<SparkleIcon />}
          loading={submitting}
          disabled={submitDisabled}
          onClick={onSubmit}
        >
          {submitLabel}
        </Button>
      </Flex>
      <Flex align="center" justify="flex-end" gap={token.marginXS} wrap="wrap">
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {submitOnEnter ? 'or press Enter (Shift+Enter for a new line)' : `or press ${submitShortcutLabel()}`}
        </Typography.Text>
      </Flex>
      {showExamples && value.trim() === '' && (
        <Flex vertical gap={token.marginXS} style={{ marginTop: token.marginXS }}>
          <SectionLabel>Or start from an example</SectionLabel>
          {EXAMPLE_QUESTIONS.map(example => (
            <Button
              key={example}
              block
              onClick={() => onChange(example)}
              style={{
                height: 'auto',
                whiteSpace: 'normal',
                textAlign: 'left',
                justifyContent: 'flex-start',
                padding: `${token.paddingXS}px ${token.paddingSM}px`,
              }}
            >
              <Typography.Text type="secondary" style={{ fontFamily: token.fontFamilyCode }}>
                &ldquo;
              </Typography.Text>{' '}
              {example}
            </Button>
          ))}
        </Flex>
      )}
    </Flex>
  );
}
