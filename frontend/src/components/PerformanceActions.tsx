import React from 'react';
import { Button, Checkbox, Flex, Tooltip, Typography } from 'antd';
import ThunderboltOutlined from '@ant-design/icons/ThunderboltOutlined';
import ApartmentOutlined from '@ant-design/icons/ApartmentOutlined';
import { useWording } from '../verbosity';
import { useTokens } from './primitives';

export interface PerformanceActionsProps {
  onAccelerate: () => void;
  onExplainPlan: () => void;
  runPlan: boolean;
  onRunPlanChange: (value: boolean) => void;
  /** Something is already running for this tab. */
  disabled: boolean;
  /** No database is selected for the tab, so there's nothing to plan on. */
  noDatabase: boolean;
  accelerating: boolean;
  explaining: boolean;
}

/**
 * Accelerate and "Explain the plan", at the top of the Refine tab - the two things the panel can do
 * about a slow query with nothing but the query and the database's own plan to go on.
 *
 * "Run query plan first" is waxwing-sql's checkbox of the same name (its `AccelerateControl`): with
 * it on the database plans the query (it isn't run) and the model gets the plan as well as the SQL;
 * off, the model gets only the SQL, which is quicker and works when the database can't plan it.
 */
export function PerformanceActions({
  onAccelerate,
  onExplainPlan,
  runPlan,
  onRunPlanChange,
  disabled,
  noDatabase,
  accelerating,
  explaining,
}: PerformanceActionsProps) {
  const token = useTokens();
  const say = useWording();
  return (
    <Flex
      vertical
      gap={token.marginXS}
      style={{
        padding: token.paddingSM,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadiusLG,
        background: token.colorFillQuaternary,
      }}
    >
      <Flex align="center" gap={token.marginXS} wrap="wrap">
        <Tooltip
          title={say(
            'Suggest a faster query that returns the same result. Nothing changes until you apply it.',
            'A faster query, same result',
          )}
        >
          <Button
            icon={<ThunderboltOutlined />}
            onClick={onAccelerate}
            disabled={disabled}
            loading={accelerating}
          >
            Accelerate
          </Button>
        </Tooltip>
        <Tooltip
          title={
            noDatabase
              ? say('Pick a database for this tab to plan the query', 'Pick a database first')
              : say('The database plans the query (it is not run) and the model gets the plan too', "Give the model the database's plan")
          }
        >
          <Checkbox
            checked={runPlan && !noDatabase}
            disabled={disabled || noDatabase}
            onChange={event => onRunPlanChange(event.target.checked)}
          >
            <Typography.Text style={{ fontSize: token.fontSizeSM }}>Run query plan first</Typography.Text>
          </Checkbox>
        </Tooltip>
      </Flex>
      <Flex align="center" gap={token.marginXS}>
        <Tooltip
          title={
            noDatabase
              ? say('Pick a database for this tab first', 'Pick a database first')
              : say('Ask the database how it would run this query, and explain it', 'How the database would run it')
          }
        >
          <Button
            size="small"
            type="link"
            icon={<ApartmentOutlined />}
            style={{ padding: 0 }}
            onClick={onExplainPlan}
            disabled={disabled || noDatabase}
            loading={explaining}
          >
            Explain the query plan
          </Button>
        </Tooltip>
      </Flex>
    </Flex>
  );
}
