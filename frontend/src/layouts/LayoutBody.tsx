import React from 'react';
import { Flex } from 'antd';
import { useTokens } from '../components/primitives';
import type { PanelModel } from './layout';

export interface LayoutBodyProps {
  model: PanelModel;
  /** No padding above, for a layout with something of its own drawn right on top (the tabs). */
  flushTop?: boolean;
  children: React.ReactNode;
}

/** The part of the panel that scrolls - between whatever a layout puts above it and the footer -
 * with the last call's failure, if any, at its top. Every layout so far uses it. */
export function LayoutBody({ model, flushTop = false, children }: LayoutBodyProps) {
  const token = useTokens();
  return (
    <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: token.paddingSM, paddingTop: flushTop ? 0 : token.paddingSM }}>
      <Flex vertical gap={token.marginSM}>
        {model.error}
        {children}
      </Flex>
    </div>
  );
}
