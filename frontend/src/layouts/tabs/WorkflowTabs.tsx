import React, { useState } from 'react';
import { Badge, Flex, Spin, Tooltip } from 'antd';
import LoadingOutlined from '@ant-design/icons/LoadingOutlined';
import type { WorkflowSegment } from '../../tabState';
import { useTokens } from '../../components/primitives';
import type { WorkflowView } from '../layout';

export interface WorkflowTabsProps {
  value: WorkflowSegment;
  onChange: (segment: WorkflowSegment) => void;
  workflows: Record<WorkflowSegment, WorkflowView>;
}

/**
 * The four workflows, one of which owns the column at a time.
 *
 * Every tab carries its own state, because switching away from a workflow is not the same as it
 * stopping: a spinner for one that's still running, a red count for problems waiting to be
 * explained, a dot for a result that arrived while the user was elsewhere. Without that, work
 * happening off screen is work the user has no way to know about.
 */
export function WorkflowTabs({ value, onChange, workflows }: WorkflowTabsProps) {
  const token = useTokens();

  return (
    <div style={{ flexShrink: 0, padding: `${token.paddingXS}px ${token.paddingSM}px` }}>
      <Flex
        role="tablist"
        gap={2}
        style={{ padding: 2, borderRadius: token.borderRadius, background: token.colorFillTertiary }}
      >
        {SEGMENTS.map(segment => (
          <Tab key={segment} workflow={workflows[segment]} selected={segment === value} onSelect={() => onChange(segment)} />
        ))}
      </Flex>
    </div>
  );
}

const SEGMENTS: WorkflowSegment[] = ['ask', 'refine', 'fix', 'describe'];

/**
 * One tab, with what it does as its tooltip - four labels in 400px leaves no room to say any of it
 * on the tab itself. The selected one is filled with the theme's primary colour, in bold, rather than the
 * faint raised chip antd's `Segmented` draws - which on Superset's light theme was barely
 * distinguishable from the tabs beside it.
 */
function Tab({ workflow, selected, onSelect }: { workflow: WorkflowView; selected: boolean; onSelect: () => void }) {
  const token = useTokens();
  const [hovered, setHovered] = useState(false);
  const { label, hint, running, unread, count } = workflow;
  return (
    <Tooltip title={hint} mouseEnterDelay={0.6}>
      <button
        type="button"
        role="tab"
        aria-selected={selected}
        onClick={onSelect}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: token.marginXXS,
          height: 26,
          padding: `0 ${token.paddingXXS}px`,
          border: 'none',
          borderRadius: token.borderRadiusSM,
          cursor: 'pointer',
          fontFamily: token.fontFamily,
          fontSize: token.fontSizeSM,
          fontWeight: selected ? 600 : 400,
          color: selected ? token.colorTextLightSolid : hovered ? token.colorText : token.colorTextSecondary,
          background: selected ? token.colorPrimary : hovered ? token.colorFillSecondary : 'transparent',
          boxShadow: selected ? token.boxShadowTertiary : undefined,
          transition: 'background 120ms ease, color 120ms ease',
        }}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
        {running ? (
          <Spin indicator={<LoadingOutlined spin style={{ color: 'inherit' }} />} size="small" />
        ) : count > 0 ? (
          <Badge count={count} size="small" />
        ) : unread ? (
          // Deliberately not a count: what's waiting is one result, and "there is something here"
          // is the whole message.
          <Badge dot status="processing" />
        ) : null}
      </button>
    </Tooltip>
  );
}
