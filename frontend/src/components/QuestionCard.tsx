import React from 'react';
import { Button, Flex, Typography } from 'antd';
import { useTokens } from './primitives';

export interface QuestionCardProps {
  question: string;
  onEdit: () => void;
  editDisabled: boolean;
}

/**
 * The question as asked, held on screen for the whole of the draft/review step so what the SQL
 * below is answering is never in doubt. "Edit question" hands it back to the composer.
 */
export function QuestionCard({ question, onEdit, editDisabled }: QuestionCardProps) {
  const token = useTokens();
  return (
    <Flex
      vertical
      gap={token.marginXS}
      style={{
        padding: token.paddingSM,
        background: token.colorFillQuaternary,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadiusLG,
      }}
    >
      <Typography.Text style={{ whiteSpace: 'pre-wrap' }}>{question}</Typography.Text>
      <Button
        type="link"
        size="small"
        disabled={editDisabled}
        onClick={onEdit}
        style={{ alignSelf: 'flex-start', padding: 0, height: 'auto' }}
      >
        Edit question
      </Button>
    </Flex>
  );
}
