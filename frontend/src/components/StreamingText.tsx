import React, { useEffect, useRef } from 'react';
import { Typography } from 'antd';
import { useTokens } from './primitives';
import { Prose } from './Prose';

export interface StreamingTextProps {
  text: string;
  /** Monospace and left-aligned for SQL; ordinary prose otherwise. */
  code?: boolean;
  /** Shown in place of the text before any has arrived. */
  placeholder?: string;
  maxHeight?: number;
}

/**
 * Text arriving a token at a time, scrolled to keep its tail in view - what the model is writing,
 * shown as it writes it rather than after it has finished.
 *
 * Only follows the text while the view is already at the bottom, so scrolling up to read something
 * further back isn't fought by the next token that arrives.
 */
export function StreamingText({ text, code = false, placeholder, maxHeight = 220 }: StreamingTextProps) {
  const token = useTokens();
  const ref = useRef<HTMLDivElement | null>(null);
  const following = useRef(true);

  useEffect(() => {
    const element = ref.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [text]);

  if (!text && placeholder) {
    return (
      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
        {placeholder}
      </Typography.Text>
    );
  }

  return (
    <div
      ref={ref}
      onScroll={() => {
        const element = ref.current;
        if (!element) return;
        // A couple of pixels of slack: a fractional scroll height otherwise reads as "scrolled up".
        following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 4;
      }}
      style={{
        maxHeight,
        overflowY: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: code ? 'break-all' : 'normal',
        fontFamily: code ? token.fontFamilyCode : undefined,
        fontSize: token.fontSizeSM,
        lineHeight: token.lineHeight,
        color: token.colorText,
        background: token.colorFillQuaternary,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadius,
        padding: `${token.paddingXS}px ${token.paddingSM}px`,
      }}
    >
      {code ? text : <Prose text={text} />}
    </div>
  );
}
