import React, { useState } from 'react';
import { Button, Flex, Modal, Popconfirm, Spin, Tag, Typography } from 'antd';
import CheckCircleFilled from '@ant-design/icons/CheckCircleFilled';
import DatabaseOutlined from '@ant-design/icons/DatabaseOutlined';
import ReloadOutlined from '@ant-design/icons/ReloadOutlined';
import { fetchDbKnowledge, RequestContext } from '../api';
import {
  forgetKnowledge,
  KnowledgeTable,
  refreshKnowledge,
  startInterrogation,
  stopInterrogation,
  useSchemaKnowledge,
} from '../schemaKnowledge';
import { useWording } from '../verbosity';
import { ProgressPanel, useElapsedMs } from './ProgressPanel';
import { Callout, Flag, SectionLabel, ToneIcon, useTokens } from './primitives';

function ago(epochSeconds: number): string {
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 90) return 'just now';
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 36 * 3600) return `${Math.round(seconds / 3600)} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function compactRows(rows: number): string {
  if (rows >= 1_000_000) return `${(rows / 1_000_000).toFixed(1)}M`;
  if (rows >= 1_000) return `${(rows / 1_000).toFixed(1)}k`;
  return String(rows);
}

export interface SchemaKnowledgeBarProps {
  /** The tab's database/catalog; null when the tab has no database. */
  context: RequestContext | null;
  /** The database's name, for naming it before the backend has said. */
  databaseLabel: string | null;
  disabled?: boolean;
}

function plural(count: number, word: string, many = `${word}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? word : many}`;
}

/**
 * Whether the assistant knows the tab's database: "Interrogate DB" when it doesn't, what it knows
 * (and since when) when it does. What it knows - every schema's tables, columns, keys, indexes and
 * sizes - goes, whole, with every question about the database (see the backend's
 * `schema_knowledge`), so this sits at the top of the two workflows that write SQL from words - Ask
 * and Refine.
 */
export function SchemaKnowledgeBar({ context, databaseLabel, disabled }: SchemaKnowledgeBarProps) {
  const token = useTokens();
  const entry = useSchemaKnowledge(context);
  const elapsed = useElapsedMs(entry.running?.startedAt ?? null);
  const [viewing, setViewing] = useState(false);
  const say = useWording();
  if (!context || context.databaseId === null) return null;

  const status = entry.status;
  const database = (status && status.databaseName) || databaseLabel || 'this database';
  const box: React.CSSProperties = {
    padding: `${token.paddingXS}px ${token.paddingSM}px`,
    borderRadius: token.borderRadius,
    border: `1px solid ${token.colorBorderSecondary}`,
    background: token.colorFillQuaternary,
  };

  if (entry.running) {
    return (
      <div style={box}>
        <ProgressPanel
          kind="interrogate"
          detail={say(
            `Interrogating ${database}: every schema's tables, columns, keys and indexes, and roughly how many rows each table has - from the database's catalog and planner, never the data.`,
            `Interrogating ${database} - structure only, never the data.`,
          )}
          progress={entry.running.progress}
          elapsedMs={elapsed}
          onStop={() => stopInterrogation(context)}
        />
      </div>
    );
  }

  const error = entry.runError && <Flag tone="error">{entry.runError}</Flag>;

  if (!status && entry.loadError) {
    return (
      <Callout tone="warning" style={{ flexDirection: 'row', alignItems: 'center' }}>
        <ToneIcon tone="warning" />
        <Typography.Text style={{ fontSize: token.fontSizeSM, flex: 1 }}>
          Couldn't check what's known about {database}: {entry.loadError}
        </Typography.Text>
        <Button size="small" type="text" icon={<ReloadOutlined />} aria-label="Check again" onClick={() => refreshKnowledge(context)} />
      </Callout>
    );
  }

  if (!status) {
    return (
      <Flex align="center" gap={token.marginXS} style={box}>
        {entry.loading ? <Spin size="small" /> : <DatabaseOutlined style={{ color: token.colorTextTertiary }} />}
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM, flex: 1 }}>
          Checking what's known about {database}…
        </Typography.Text>
      </Flex>
    );
  }

  if (status.status === 'none') {
    return (
      <Callout tone="warning" gap={token.marginXXS}>
        <Flex align="center" gap={token.marginXS}>
          <ToneIcon tone="warning" />
          <Typography.Text style={{ fontSize: token.fontSizeSM, flex: 1, fontWeight: 500 }}>
            {say("The assistant hasn't been told about the tables in ", 'Not yet told about ')}
            <Typography.Text code>{database}</Typography.Text>.
          </Typography.Text>
          <Button size="small" type="primary" ghost disabled={disabled} onClick={() => startInterrogation(context)}>
            Interrogate DB
          </Button>
        </Flex>
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          {say(
            "Reads the structure of every schema - tables, columns, keys, indexes, and roughly how many rows each table has (the planner's estimate; nothing is run). That then goes with every question about this database, for everyone using it.",
            'Reads the structure only - nothing is run. Shared by all.',
          )}
        </Typography.Text>
        {error}
      </Callout>
    );
  }

  return (
    <Flex vertical gap={token.marginXXS} style={box}>
      <Flex align="center" gap={token.marginXS}>
        <CheckCircleFilled style={{ color: token.colorSuccess }} />
        <Typography.Text style={{ fontSize: token.fontSizeSM, flex: 1, minWidth: 0 }} ellipsis>
          The assistant knows <Typography.Text code>{status.databaseName}</Typography.Text>
        </Typography.Text>
      </Flex>
      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
        {plural(status.tableCount, 'table')}
        {status.viewCount > 0 ? `, ${plural(status.viewCount, 'view')}` : ''} in {plural(status.schemaCount, 'schema')}
        {say(
          <>
            {status.indexCount > 0 ? ` · ${plural(status.indexCount, 'index', 'indexes')}` : ''}
            {status.sizedCount > 0 ? ` · ${status.sizedCount.toLocaleString()} sized` : ''}
            {status.described ? ` · ${status.describedCount.toLocaleString()} described` : ''}
            {' · interrogated '}
            {ago(status.createdAt)}
            {status.createdBy ? ` by ${status.createdBy}` : ''}
          </>,
          ` · ${ago(status.createdAt)}`,
        )}
      </Typography.Text>
      <Flex gap={token.marginSM}>
        <Button size="small" type="link" style={{ padding: 0 }} onClick={() => setViewing(true)}>
          View
        </Button>
        <Button size="small" type="link" style={{ padding: 0 }} disabled={disabled} onClick={() => startInterrogation(context)}>
          Interrogate again
        </Button>
        <Popconfirm
          title="Forget what was found?"
          description={say(
            'Prompts about this database go back to working from the SQL alone, for everyone.',
            'Questions go back to the SQL alone, for everyone.',
          )}
          okText="Forget"
          onConfirm={() => forgetKnowledge(context)}
        >
          <Button size="small" type="link" danger style={{ padding: 0 }} disabled={disabled}>
            Forget
          </Button>
        </Popconfirm>
      </Flex>
      {status.problems.length > 0 && (
        <Flag tone="warning">{plural(status.problems.length, 'problem')} while interrogating - see View.</Flag>
      )}
      {error}
      {viewing && <KnowledgeModal context={context} title={status.databaseName} onClose={() => setViewing(false)} />}
    </Flex>
  );
}

/** How many tables the modal draws at once - a database can have thousands. */
const MODAL_TABLE_LIMIT = 150;

/** Everything the assistant was told about the database - what goes with each question. */
function KnowledgeModal({ context, title, onClose }: { context: RequestContext; title: string; onClose: () => void }) {
  const token = useTokens();
  const [tables, setTables] = useState<KnowledgeTable[] | null>(null);
  const [extra, setExtra] = useState<{ described: boolean; problems: string[]; model: string | null; skipped: number }>({
    described: false,
    problems: [],
    model: null,
    skipped: 0,
  });
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const say = useWording();
  React.useEffect(() => {
    fetchDbKnowledge(context, true).then(
      status => {
        if (status.status === 'ready') {
          setTables(status.tables ?? []);
          setExtra({ described: status.described, problems: status.problems, model: status.model, skipped: status.skippedSchemas });
        } else {
          setTables([]);
        }
      },
      e => setError(e instanceof Error ? e.message : 'Could not load it'),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const needle = filter.trim().toLowerCase();
  const matching = (tables ?? []).filter(
    table =>
      !needle ||
      `${table.schema}.${table.name}`.toLowerCase().includes(needle) ||
      (table.description ?? '').toLowerCase().includes(needle) ||
      table.columns.some(column => column.name.toLowerCase().includes(needle)),
  );
  const shown = matching.slice(0, MODAL_TABLE_LIMIT);
  return (
    <Modal open title={`What the assistant knows about ${title}`} onCancel={onClose} footer={null} width={760}>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      {!tables && !error && <Spin />}
      {tables && (
        <Flex vertical gap={token.marginSM} style={{ maxHeight: '65vh', overflowY: 'auto' }}>
          <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
            {say(
              <>
                Read from the database's catalog - names, types, keys and indexes - with row counts from the planner's
                estimates.
                {extra.described
                  ? ` The descriptions were written by ${extra.model ?? 'the model'} from that structure alone - it never saw the data - so treat each as likely, not certain.`
                  : ''}{' '}
                All of this goes with every question asked about the database.
                {extra.skipped > 0 ? ` (${plural(extra.skipped, 'schema')} belonging to the database itself left out.)` : ''}
              </>,
              extra.described
                ? 'From the catalog; the descriptions are the model’s guesses.'
                : 'From the catalog; sent with every question.',
            )}
          </Typography.Text>
          <input
            placeholder="Filter by schema, table or column"
            value={filter}
            onChange={event => setFilter(event.target.value)}
            style={{
              padding: `${token.paddingXXS}px ${token.paddingXS}px`,
              border: `1px solid ${token.colorBorder}`,
              borderRadius: token.borderRadius,
              background: token.colorBgContainer,
              color: token.colorText,
            }}
          />
          {matching.length > shown.length && (
            <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
              Showing {shown.length} of {matching.length.toLocaleString()} - filter to find the rest.
            </Typography.Text>
          )}
          {shown.map(table => (
            <Flex
              key={`${table.schema}.${table.name}`}
              vertical
              gap={token.marginXXS}
              style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, paddingTop: token.paddingXS }}
            >
              <Flex align="center" gap={token.marginXS} wrap="wrap">
                <Typography.Text strong style={{ fontFamily: token.fontFamilyCode }}>
                  <span style={{ color: token.colorTextTertiary, fontWeight: 'normal' }}>{table.schema}.</span>
                  {table.name}
                </Typography.Text>
                {table.kind === 'view' && <Tag>view</Tag>}
                {table.rowCountEstimate !== undefined && <Tag>≈{compactRows(table.rowCountEstimate)} rows</Tag>}
                {(table.synonyms ?? []).map(word => (
                  <Tag key={word} color="blue">
                    {word}
                  </Tag>
                ))}
              </Flex>
              {table.description && <Typography.Text style={{ fontSize: token.fontSizeSM }}>{table.description}</Typography.Text>}
              <div style={{ fontSize: token.fontSizeSM - 1, fontFamily: token.fontFamilyCode, color: token.colorTextSecondary }}>
                {table.columns.map(column => (
                  <div key={column.name}>
                    {column.name} <span style={{ color: token.colorTextTertiary }}>{column.dataType}</span>
                    {column.isPrimaryKey ? ' PK' : ''}
                    {column.description ? (
                      <span style={{ fontFamily: token.fontFamily, color: token.colorText }}> - {column.description}</span>
                    ) : null}
                  </div>
                ))}
              </div>
              {(table.indexes ?? []).length > 0 && (
                <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                  Indexes:{' '}
                  {(table.indexes ?? [])
                    .map(index => `${index.unique ? 'unique ' : ''}(${index.columns.join(', ')})`)
                    .join('; ')}
                </Typography.Text>
              )}
              {(table.relationships ?? []).length > 0 && (
                <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                  Joins: {(table.relationships ?? []).map(relationship => relationship.description).join('; ')}
                </Typography.Text>
              )}
            </Flex>
          ))}
          {extra.problems.length > 0 && (
            <Flex vertical gap={token.marginXXS}>
              <SectionLabel>Problems</SectionLabel>
              {extra.problems.map(problem => (
                <Typography.Text key={problem} type="secondary" style={{ fontSize: token.fontSizeSM }}>
                  {problem}
                </Typography.Text>
              ))}
            </Flex>
          )}
        </Flex>
      )}
    </Modal>
  );
}
