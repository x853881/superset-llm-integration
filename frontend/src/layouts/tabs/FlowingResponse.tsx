import React, { useLayoutEffect, useRef } from 'react';
import { Flex } from 'antd';
import { useTokens } from '../../components/primitives';

/**
 * Alpha's response area, the same on every tab: the response reads from its top down, with the one
 * scrollbar when it doesn't fit. Nothing on screen is moved once it's there - the view doesn't
 * follow the text as it streams, nor glide to the result when it lands (its progress kept, the
 * result arriving under it); only a new call starts back at the top.
 */
export function FlowingResponse({
  running,
  children,
  containerRef,
  style,
}: {
  /** Whether the call this shows is in flight. */
  running: boolean;
  children: React.ReactNode;
  containerRef?: React.RefObject<HTMLDivElement | null>;
  style?: React.CSSProperties;
}) {
  const token = useTokens();
  const own = useRef<HTMLDivElement>(null);
  const ref = containerRef ?? own;
  const wasRunning = useRef(running);

  useLayoutEffect(() => {
    const started = !wasRunning.current && running;
    wasRunning.current = running;
    if (started && ref.current) ref.current.scrollTop = 0;
  });

  return (
    <div ref={ref} style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowAnchor: 'none', ...style }}>
      <Flex vertical gap={token.marginSM}>
        {children}
      </Flex>
    </div>
  );
}
