import React, { useState } from 'react';
import { Button, Dropdown, Flex, Modal, Typography } from 'antd';
import CheckOutlined from '@ant-design/icons/CheckOutlined';
import MoreOutlined from '@ant-design/icons/MoreOutlined';
import { THEMES, setPanelTheme, usePanelTheme } from '../panelTheme';
import { SparkleIcon, useTokens } from './primitives';

export interface PanelHeaderProps {
  /** What the assistant knows about the tab's database - offered from the ⋯
   * menu when the layout leaves it out of its own body (see `LayoutDefinition.schemaBarInMenu`). */
  schemaBar?: React.ReactNode;
}

/**
 * The panel's own title bar - the host renders extension views bare, with no chrome of its own,
 * so this is what identifies the panel in SQL Lab's right sidebar.
 *
 * The ⋯ menu holds what belongs to the panel as a whole rather than to any one workflow: what the
 * assistant knows about the database, and the theme (see `panelTheme`) - the only place it's
 * changed.
 */
export function PanelHeader({ schemaBar }: PanelHeaderProps) {
  const token = useTokens();
  const current = usePanelTheme();
  const [schemaOpen, setSchemaOpen] = useState(false);
  const onMenu = (key: string) => {
    if (key.startsWith('theme:')) setPanelTheme(key.slice('theme:'.length));
    else if (key === 'interrogate') setSchemaOpen(true);
  };
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
        <Typography.Text strong style={{ fontSize: token.fontSizeLG }}>
          AI Assistant
        </Typography.Text>
      </Flex>
      <Flex align="center" gap={token.marginXXS}>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              ...(schemaBar ? [{ key: 'interrogate', label: 'Database knowledge…' }] : []),
              {
                key: 'theme',
                label: 'Change theme',
                children: THEMES.map(option => ({
                  key: `theme:${option.id}`,
                  label: option.name,
                  title: option.description,
                  icon: <CheckOutlined style={{ visibility: option.id === current.id ? 'visible' : 'hidden' }} />,
                })),
              },
            ],
            onClick: ({ key }) => onMenu(key),
          }}
        >
          <Button type="text" size="small" icon={<MoreOutlined />} aria-label="More actions" />
        </Dropdown>
        {schemaBar && (
          <Modal title="Database knowledge" open={schemaOpen} onCancel={() => setSchemaOpen(false)} footer={null} width={520}>
            {schemaBar}
          </Modal>
        )}
      </Flex>
    </Flex>
  );
}
