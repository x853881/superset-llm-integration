import React from 'react';
import { Button, Flex, Input, Tooltip } from 'antd';
import ArrowUpOutlined from '@ant-design/icons/ArrowUpOutlined';
import { useTokens } from './primitives';

export interface RefineComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  disabled: boolean;
  submitting: boolean;
}

/**
 * The follow-up box, pinned below the result: a change to make to the SQL that's already in the
 * editor, expressed the same way the original question was. Enter sends it, Shift+Enter starts a
 * new line - the usual way round for a single-line composer.
 */
export function RefineComposer({ value, onChange, onSubmit, disabled, submitting }: RefineComposerProps) {
  const token = useTokens();
  const canSubmit = !disabled && !submitting && value.trim() !== '';
  return (
    <Flex gap={token.marginXS} align="flex-end">
      <Input.TextArea
        value={value}
        onChange={event => onChange(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            if (canSubmit) onSubmit();
          }
        }}
        disabled={disabled}
        autoSize={{ minRows: 1, maxRows: 5 }}
        placeholder={'Refine this query \u2014 e.g. \u201conly the top 5\u201d'}
      />
      <Tooltip title="Refine the SQL in the editor">
        <Button
          type="primary"
          icon={<ArrowUpOutlined />}
          loading={submitting}
          disabled={!canSubmit}
          onClick={onSubmit}
          aria-label="Refine the SQL in the editor"
        />
      </Tooltip>
    </Flex>
  );
}
