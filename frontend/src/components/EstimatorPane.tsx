import React, { useEffect, useState } from 'react';
import { Button, Flex, Spin, Tooltip, Typography } from 'antd';
import DownOutlined from '@ant-design/icons/DownOutlined';
import ExclamationCircleFilled from '@ant-design/icons/ExclamationCircleFilled';
import InfoCircleOutlined from '@ant-design/icons/InfoCircleOutlined';
import RightOutlined from '@ant-design/icons/RightOutlined';
import ThunderboltOutlined from '@ant-design/icons/ThunderboltOutlined';
import { components } from '@apache-superset/core';
import { compactNumber, estimate, impactOf, ImpactLevel, presentWarning, useEstimate, useFillHeight, useVisible, walk } from '../estimate';
import type { PlanNode, PlanSummary } from '../performanceResult';
import { contextFor, useActiveSqlLabTab, useTabEditor } from '../sqlLabHooks';
import { isSqlEmpty } from '../tabState';
import { useWording } from '../verbosity';
import { SectionLabel, Tokens, useTokens } from './primitives';

function levelColour(level: ImpactLevel, token: Tokens): string {
  switch (level) {
    case 'Low':
      return token.colorSuccess;
    case 'Moderate':
      return token.colorWarning;
    case 'High':
      return (token as unknown as Record<string, string>).orange ?? token.colorWarning;
    case 'Critical':
      return token.colorError;
    default:
      return token.colorTextQuaternary;
  }
}

/**
 * The "Query Estimator" tab under SQL Lab's editor: how heavy the editor's query looks to the
 * database's planner - an impact level, the estimated rows and cost, what's worth worrying about,
 * and the plan itself as a tree. The query is planned (`EXPLAIN`), never run, so there is no time.
 *
 * Planned when the tab comes into view with SQL it hasn't estimated, and on "Re-estimate" - not on
 * every keystroke, since each plan is a call to the database and a line in the user's query history.
 */
export function EstimatorPane() {
  const token = useTokens();
  const say = useWording();
  const activeTab = useActiveSqlLabTab();
  const { editor, sql } = useTabEditor(activeTab);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const visible = useVisible(root);
  const fillHeight = useFillHeight(visible ? root : null);
  const entry = useEstimate(activeTab?.id ?? '');
  const current = sql.trim();
  const databaseId = activeTab?.databaseId ?? null;
  const hasSql = !isSqlEmpty(current);
  const est = entry.estimate;
  const stale = est !== null && (est.sql !== current || est.databaseId !== databaseId);

  useEffect(() => {
    if (!visible || !activeTab || !editor || !hasSql || databaseId === null || entry.loading) return;
    if (est && !stale) return;
    // Only by itself when nothing has been estimated for this tab's current SQL - once there's an
    // estimate, a changed query waits for "Re-estimate".
    if (est && est.sql !== current && est.databaseId === databaseId) return;
    estimate(current, contextFor(activeTab));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, activeTab?.id, editor, databaseId]);

  const reestimate = () => activeTab && hasSql && estimate(current, contextFor(activeTab));

  let body: React.ReactNode;
  if (!activeTab) body = <Typography.Text type="secondary">Open a SQL Lab tab to estimate its query.</Typography.Text>;
  else if (databaseId === null) body = <Typography.Text type="secondary">Pick a database for this tab to estimate its query.</Typography.Text>;
  else if (!hasSql) body = <Typography.Text type="secondary">Write or paste a query in the editor to estimate it.</Typography.Text>;
  else if (!est)
    body = (
      <Flex gap={token.marginXS} align="center">
        <Spin size="small" />
        <Typography.Text type="secondary">Asking the planner…</Typography.Text>
      </Flex>
    );
  else if (!est.plan)
    body = (
      <components.Alert type={est.checked ? 'error' : 'warning'} showIcon closable={false}>
        {est.checked ? 'The database rejected the query: ' : "Couldn't estimate it: "}
        {est.error}
      </components.Alert>
    );
  else body = <Estimate plan={est.plan} />;

  return (
    <div
      ref={setRoot}
      style={{ height: fillHeight ?? 'auto', minHeight: 220, overflowY: 'auto', padding: `0 ${token.paddingXS}px` }}
    >
      <Flex align="center" justify="flex-end" gap={token.marginSM} style={{ paddingBottom: token.paddingXS }}>
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM, flex: 1 }}>
          {stale && est
            ? 'The query has changed since this estimate.'
            : est?.plan
              ? say(
                  `The planner's estimate (EXPLAIN) - the query isn't run.${est.plan.durationMs !== undefined ? ` Planned in ${est.plan.durationMs} ms.` : ''}`,
                  "The planner's estimate - not run.",
                )
              : ''}
        </Typography.Text>
        <Button icon={<ThunderboltOutlined />} loading={entry.loading} disabled={!hasSql || databaseId === null} onClick={reestimate}>
          Re-estimate
        </Button>
      </Flex>
      <div style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, paddingTop: token.paddingSM, opacity: stale ? 0.6 : 1 }}>
        {body}
      </div>
    </div>
  );
}

function Estimate({ plan }: { plan: PlanSummary }) {
  const token = useTokens();
  const say = useWording();
  const impact = impactOf(plan);
  const colour = levelColour(impact.level, token);
  // The first step down from the top that has an estimate: Trino's top step ("Fragments") has none.
  const rows = (plan.tree ? walk(plan.tree).find(node => node.estimatedRows !== null)?.estimatedRows : null) ?? null;
  const column: React.CSSProperties = { flex: '1 1 260px', minWidth: 240, padding: `0 ${token.paddingMD}px` };
  const divider = { borderLeft: `1px solid ${token.colorBorderSecondary}` };
  return (
    <Flex wrap="wrap" style={{ rowGap: token.marginLG }}>
      <div style={column}>
        <SectionLabel>Resource impact</SectionLabel>
        <Tooltip title={impact.reason}>
          <Flex align="center" gap={token.marginSM} style={{ margin: `${token.marginSM}px 0 ${token.marginLG}px` }}>
            <div style={{ flex: 1, height: 8, borderRadius: 4, background: token.colorFillSecondary, overflow: 'hidden' }}>
              <div style={{ width: `${Math.max(impact.fill, 0.04) * 100}%`, height: '100%', background: colour, borderRadius: 4 }} />
            </div>
            {/* The bar carries the colour; the word stays readable - yellow and orange text are not. */}
            <Typography.Text style={{ fontSize: token.fontSizeLG }}>{impact.level}</Typography.Text>
          </Flex>
        </Tooltip>
        <SectionLabel>Metrics</SectionLabel>
        <Metric label="Rows" value={rows !== null ? compactNumber(rows) : 'N/A'} hint={say("The planner's estimate of the rows the query returns.", "The planner's estimate")} />
        <Metric
          label="Cost"
          value={plan.totalCost !== null ? compactNumber(plan.totalCost) : 'N/A'}
          hint={
            plan.totalCost !== null
              ? say(`In ${plan.costUnit} - comparable between queries on this database, not a time.`, `In ${plan.costUnit}, not a time`)
              : say('This database gives no cost estimate.', 'None from this database')
          }
        />
        {plan.estimatedSeconds !== null && plan.estimatedSeconds !== undefined ? (
          <Metric
            label="Time"
            value={`≈${plan.estimatedSeconds < 1 ? '<1' : plan.estimatedSeconds.toLocaleString()} s`}
            hint={say("The planner's own estimate of the run time - the query isn't run.", "The planner's estimate - not run")}
          />
        ) : (
          <Metric
            label="Time"
            value="N/A"
            hint={say('The query is planned, never run, and this database gives no time estimate.', 'None from this database')}
          />
        )}
        <Metric label="Plan steps" value={plan.steps !== null ? String(plan.steps) : 'N/A'} />
      </div>

      <div style={{ ...column, ...divider }}>
        <SectionLabel>Warnings ({plan.warnings.length})</SectionLabel>
        {plan.warnings.length === 0 && (
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: token.marginSM }}>
            Nothing stands out in the plan.
          </Typography.Text>
        )}
        <Flex vertical gap={token.marginMD} style={{ marginTop: token.marginSM }}>
          {plan.warnings.map((warning, index) => {
            const presentation = presentWarning(warning.code);
            const isWarning = warning.severity === 'warning';
            return (
              // eslint-disable-next-line react/no-array-index-key
              <Flex key={index} gap={token.marginXS} align="flex-start">
                {isWarning ? (
                  <ExclamationCircleFilled style={{ color: token.colorError, marginTop: 4 }} />
                ) : (
                  <InfoCircleOutlined style={{ color: token.colorInfo, marginTop: 4 }} />
                )}
                <Flex vertical gap={token.marginXXS}>
                  <Typography.Text strong style={{ fontSize: token.fontSizeLG }}>
                    {presentation.title}
                  </Typography.Text>
                  <Typography.Text style={{ fontSize: token.fontSizeSM }}>{warning.message}</Typography.Text>
                  <Typography.Text style={{ fontSize: token.fontSizeSM, color: token.colorPrimary }}>{presentation.suggestion}</Typography.Text>
                </Flex>
              </Flex>
            );
          })}
        </Flex>
      </div>

      <div style={{ ...column, ...divider, flex: '2 1 360px' }}>
        <SectionLabel>Query plan</SectionLabel>
        <div style={{ marginTop: token.marginSM, overflowX: 'auto' }}>
          {plan.tree ? (
            <PlanTree node={plan.tree} depth={0} />
          ) : (
            <pre style={{ fontFamily: token.fontFamilyCode, fontSize: token.fontSizeSM, whiteSpace: 'pre', margin: 0 }}>{plan.outline}</pre>
          )}
        </div>
      </div>
    </Flex>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  const token = useTokens();
  return (
    <Tooltip title={hint} placement="left">
      <Flex justify="space-between" style={{ padding: `${token.paddingXS}px 0`, borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Typography.Text>{label}</Typography.Text>
        <Typography.Text strong>{value}</Typography.Text>
      </Flex>
    </Tooltip>
  );
}

function PlanTree({ node, depth }: { node: PlanNode; depth: number }) {
  const token = useTokens();
  const [open, setOpen] = useState(depth < 8);
  const [showDetails, setShowDetails] = useState(false);
  const hasChildren = node.children.length > 0;
  const numbers = [
    node.estimatedRows !== null ? `${compactNumber(node.estimatedRows)} rows` : null,
    node.cost !== null ? `cost: ${compactNumber(node.cost)}` : null,
  ].filter(Boolean);
  const details = Object.entries(node.details).filter(([, value]) => value && value !== '[]');
  return (
    <div>
      <Flex align="center" gap={token.marginXS} style={{ padding: '3px 0', whiteSpace: 'nowrap' }}>
        <span
          role={hasChildren ? 'button' : undefined}
          onClick={() => hasChildren && setOpen(!open)}
          style={{ width: 14, color: token.colorTextSecondary, cursor: hasChildren ? 'pointer' : 'default', fontSize: 10 }}
        >
          {hasChildren ? open ? <DownOutlined /> : <RightOutlined /> : <span style={{ color: token.colorBorder }}>└</span>}
        </span>
        <Typography.Text
          strong
          onClick={() => details.length && setShowDetails(!showDetails)}
          style={{ cursor: details.length ? 'pointer' : 'default' }}
        >
          {node.operation}
        </Typography.Text>
        {node.objectName && <Typography.Text style={{ color: token.colorPrimary }}>{node.objectName}</Typography.Text>}
        {numbers.length > 0 && <Typography.Text type="secondary">({numbers.join(', ')})</Typography.Text>}
        {details.length > 0 && !showDetails && (
          <Typography.Link style={{ fontSize: token.fontSizeSM }} onClick={() => setShowDetails(true)}>
            details
          </Typography.Link>
        )}
      </Flex>
      {showDetails && (
        <div style={{ marginLeft: 22, marginBottom: 4, fontSize: token.fontSizeSM, color: token.colorTextSecondary }}>
          {details.map(([key, value]) => (
            <div key={key} style={{ fontFamily: token.fontFamilyCode, whiteSpace: 'pre-wrap' }}>
              {key}: {value}
            </div>
          ))}
        </div>
      )}
      {open && hasChildren && (
        <div style={{ marginLeft: 6, paddingLeft: 14, borderLeft: `1px solid ${token.colorBorderSecondary}` }}>
          {node.children.map((child, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <PlanTree key={index} node={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}
