import React from 'react';
import { Button, Checkbox, Dropdown, Flex, Tooltip, Typography } from 'antd';
import CheckOutlined from '@ant-design/icons/CheckOutlined';
import LayoutOutlined from '@ant-design/icons/LayoutOutlined';
import MoreOutlined from '@ant-design/icons/MoreOutlined';
import { OPTIONS_SHORTCUT, setOptionsOpen } from '../devOptions';
import { LAYOUTS } from '../layouts';
import { LAYOUT_SHORTCUT, setPanelLayout, useLayout } from '../panelLayout';
import { setBrief, useBrief } from '../verbosity';
import { SparkleIcon, useTokens } from './primitives';

export interface PanelHeaderProps {
  startOverDisabled: boolean;
  onStartOver: () => void;
}

/**
 * The panel's own title bar - the host renders extension views bare, with no chrome of its own,
 * so this is what identifies the panel in SQL Lab's right sidebar.
 *
 * The overflow menu holds what belongs to the panel as a whole rather than to any one workflow:
 * clearing this tab back to a blank slate, and the options page (see `devOptions`). Beside it, the
 * layout switcher (see `panelLayout`) and the "Brief" checkbox (see `verbosity`).
 */
export function PanelHeader({ startOverDisabled, onStartOver }: PanelHeaderProps) {
  const token = useTokens();
  const current = useLayout();
  const brief = useBrief();
  return (
    <Flex
      align="center"
      justify="space-between"
      style={{
        flexShrink: 0,
        padding: `${token.paddingXS}px ${token.paddingSM}px`,
        borderBottom: `1px solid ${token.colorBorderSecondary}`,
      }}
    >
      <Flex align="center" gap={token.marginXS}>
        <SparkleIcon style={{ color: token.colorPrimary, fontSize: token.fontSizeLG }} />
        {/* Not "Ask" any more: the panel opens on Refine (see `DEFAULT_SEGMENT`), and asking is one of
            four things it does. */}
        <Typography.Text strong style={{ fontSize: token.fontSizeLG }}>
          AI Assistant
        </Typography.Text>
      </Flex>
      <Flex align="center" gap={token.marginXXS}>
        <Tooltip
          title={
            brief
              ? 'Shorter text throughout'
              : "Short descriptions and explanations from now on (what's already written stays as it is), and the panel's own help text cut to a line"
          }
        >
          <Checkbox
            checked={brief}
            onChange={event => setBrief(event.target.checked)}
            style={{ fontSize: token.fontSizeSM, color: token.colorTextSecondary, marginInlineEnd: token.marginXXS }}
          >
            Brief
          </Checkbox>
        </Tooltip>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { type: 'group', label: `Layout · ${LAYOUT_SHORTCUT} to switch` },
              ...LAYOUTS.map(option => ({
                key: option.id,
                label: option.name,
                icon: <CheckOutlined style={{ visibility: option.id === current.id ? 'visible' : 'hidden' }} />,
              })),
            ],
            onClick: ({ key }) => setPanelLayout(key),
          }}
        >
          <Button type="text" size="small" icon={<LayoutOutlined />} aria-label="Layout">
            <span style={{ fontSize: token.fontSizeSM, color: token.colorTextSecondary }}>{current.name}</span>
          </Button>
        </Dropdown>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'startOver', label: 'Start over (clears this tab)', disabled: startOverDisabled },
              { type: 'divider' },
              { key: 'options', label: `Options… (${OPTIONS_SHORTCUT})` },
            ],
            onClick: ({ key }) => (key === 'options' ? setOptionsOpen(true) : onStartOver()),
          }}
        >
          <Button type="text" size="small" icon={<MoreOutlined />} aria-label="More actions" />
        </Dropdown>
      </Flex>
    </Flex>
  );
}
