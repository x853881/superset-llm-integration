import React from 'react';
import { Flex, Typography } from 'antd';
import DatabaseOutlined from '@ant-design/icons/DatabaseOutlined';
import { ToneIcon, useTokens } from './primitives';

export interface ContextBarProps {
  databaseName: string | null;
  catalog: string | null;
}

/**
 * Where the question will be answered: the database (and catalog) the active SQL Lab tab is already
 * pointed at. The panel takes both from the tab rather than asking for them again, so this is a
 * readout, not a control - hence "from this tab". Not the tab's schema: queries here qualify every
 * table with its schema, and the assistant is told about all of them.
 */
export function ContextBar({ databaseName, catalog }: ContextBarProps) {
  const token = useTokens();
  const parts = [databaseName, catalog].filter((part): part is string => !!part);
  return (
    <Flex
      align="center"
      justify="space-between"
      gap={token.marginXS}
      style={{
        flexShrink: 0,
        padding: `${token.paddingXXS}px ${token.paddingSM}px`,
        background: token.colorFillQuaternary,
        borderBottom: `1px solid ${token.colorBorderSecondary}`,
      }}
    >
      <Flex align="center" gap={token.marginXXS} style={{ minWidth: 0 }}>
        {parts.length > 0 ? (
          <>
            <DatabaseOutlined style={{ color: token.colorTextTertiary, fontSize: token.fontSizeSM }} />
            <Typography.Text
              ellipsis
              style={{ fontFamily: token.fontFamilyCode, fontSize: token.fontSizeSM, color: token.colorTextSecondary }}
            >
              {parts.join(' / ')}
            </Typography.Text>
          </>
        ) : (
          // Nothing the panel does that touches the database can work without one, so this is a
          // warning rather than a readout.
          <>
            <ToneIcon tone="warning" style={{ fontSize: token.fontSizeSM }} />
            <Typography.Text style={{ fontSize: token.fontSizeSM, color: token.colorText }}>
              No database selected
            </Typography.Text>
          </>
        )}
      </Flex>
      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM, whiteSpace: 'nowrap' }}>
        from this tab
      </Typography.Text>
    </Flex>
  );
}
