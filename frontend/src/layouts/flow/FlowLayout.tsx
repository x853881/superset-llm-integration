import React, { useEffect, useRef, useState } from 'react';
import { Badge, Flex, Spin, Typography } from 'antd';
import LoadingOutlined from '@ant-design/icons/LoadingOutlined';
import RightOutlined from '@ant-design/icons/RightOutlined';
import type { WorkflowSegment } from '../../tabState';
import { useTokens } from '../../components/primitives';
import type { WorkflowView } from '../layout';

export interface FlowLayoutProps {
  /** In order, top to bottom; a workflow with nothing to act on is left out by the caller. Each is
   * open while it `wantsAttention`, unless the user closed it. */
  sections: WorkflowView[];
  /** The workflow last shown (the tab state's `segment`) - kept open, and scrolled to when it changes,
   * so whatever moves the panel to a workflow (undo stepping to a fix, "Edit in Ask") still lands. */
  focused: WorkflowSegment;
  onFocus: (segment: WorkflowSegment) => void;
}

/**
 * The "Single column" layout's sections (see `./index.tsx`): the four workflows as one column of sections
 * instead of tabs, after sql-helper's single linear flow. Which are open follows the SQL - Ask while
 * there is none, Fix while something is reported against it, Refine once there is some - and any
 * header can be clicked to open or close its section. Kept per SQL Lab tab: the caller keys this by
 * tab, so a tab's opened and closed sections don't carry over to the next.
 */
export function FlowLayout({ sections, focused, onFocus }: FlowLayoutProps) {
  const token = useTokens();
  const [overrides, setOverrides] = useState<Partial<Record<WorkflowSegment, boolean>>>({});
  const refs = useRef<Partial<Record<WorkflowSegment, HTMLDivElement | null>>>({});
  const first = useRef(true);

  useEffect(() => {
    setOverrides(previous => {
      const { [focused]: _dropped, ...rest } = previous;
      return rest;
    });
    if (first.current) {
      first.current = false;
      return;
    }
    refs.current[focused]?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [focused]);

  return (
    <Flex vertical gap={token.marginXS}>
      {sections.map(section => {
        const open = overrides[section.id] ?? (section.wantsAttention || section.id === focused);
        return (
          <div
            key={section.id}
            ref={element => {
              refs.current[section.id] = element;
            }}
            style={{
              border: `1px solid ${open ? token.colorBorder : token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              background: token.colorBgContainer,
            }}
          >
            <button
              type="button"
              aria-expanded={open}
              onClick={() => {
                setOverrides(previous => ({ ...previous, [section.id]: !open }));
                if (!open) onFocus(section.id);
              }}
              style={{
                width: '100%',
                display: 'flex',
                alignItems: 'center',
                gap: token.marginXS,
                padding: `${token.paddingXS}px ${token.paddingSM}px`,
                border: 'none',
                borderBottom: open ? `1px solid ${token.colorBorderSecondary}` : 'none',
                borderRadius: open ? `${token.borderRadiusLG}px ${token.borderRadiusLG}px 0 0` : token.borderRadiusLG,
                background: open ? token.colorFillQuaternary : 'transparent',
                cursor: 'pointer',
                textAlign: 'left',
                fontFamily: token.fontFamily,
              }}
            >
              <RightOutlined
                style={{
                  fontSize: token.fontSizeSM - 2,
                  color: token.colorTextTertiary,
                  transform: open ? 'rotate(90deg)' : undefined,
                  transition: 'transform 120ms ease',
                }}
              />
              <Flex vertical style={{ flex: 1, minWidth: 0 }}>
                <Typography.Text strong>{section.label}</Typography.Text>
                {!open && (
                  <Typography.Text type="secondary" ellipsis style={{ fontSize: token.fontSizeSM }}>
                    {section.hint}
                  </Typography.Text>
                )}
              </Flex>
              {section.running ? (
                <Spin indicator={<LoadingOutlined spin />} size="small" />
              ) : section.count > 0 ? (
                <Badge count={section.count} size="small" />
              ) : section.unread && !open ? (
                <Badge dot status="processing" />
              ) : null}
            </button>
            {open && (
              <Flex vertical gap={token.marginSM} style={{ padding: token.paddingSM }}>
                {section.body}
              </Flex>
            )}
          </div>
        );
      })}
    </Flex>
  );
}
