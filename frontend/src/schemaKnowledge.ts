import { useEffect, useState } from 'react';
import { cancelRequest, fetchDbKnowledge, forgetDbKnowledge, interrogateDb, isCancellation, RequestContext } from './api';
import { CallProgress, EMPTY_PROGRESS, perFrame } from './progress';

/**
 * What the extension knows about a database after "Interrogate DB" - mirrors the backend's
 * `schema_knowledge.DatabaseKnowledge.summary()`.
 */
export interface SchemaKnowledgeSummary {
  databaseId: number;
  databaseName: string;
  catalog: string | null;
  schemas: { name: string; tableCount: number }[];
  schemaCount: number;
  /** The database's own schemas (information_schema, Oracle's SYS...), left out. */
  skippedSchemas: number;
  tableCount: number;
  viewCount: number;
  columnCount: number;
  indexCount: number;
  /** Tables with a row-count estimate from the planner. */
  sizedCount: number;
  relationshipCount: number;
  /** Whether the model described the tables (``NLSQL_INTERROGATE_DESCRIPTIONS``), or it's structure alone. */
  described: boolean;
  describedCount: number;
  /** Epoch seconds. */
  createdAt: number;
  createdBy: string | null;
  model: string | null;
  problems: string[];
  durationMs: number;
}

export interface KnowledgeTable {
  name: string;
  schema: string;
  kind?: 'view';
  description?: string;
  synonyms?: string[];
  rowCountEstimate?: number;
  columns: { name: string; dataType: string; nullable: boolean; description?: string; enumValues?: string[]; isPrimaryKey?: boolean; isForeignKey?: boolean }[];
  indexes?: { columns: string[]; unique: boolean }[];
  relationships?: { description: string; fromColumn: string; toTable: string; toColumn: string }[];
}

export type SchemaKnowledgeStatus =
  | { status: 'none'; databaseName: string | null }
  | ({ status: 'ready'; tables?: KnowledgeTable[] } & SchemaKnowledgeSummary);

/**
 * One database's entry: what the backend last said about it, and any interrogation running for it.
 * Kept per database/catalog rather than per SQL Lab tab - two tabs on the same database share what
 * was found, and an interrogation started from either shows in both.
 */
export interface KnowledgeEntry {
  status: SchemaKnowledgeStatus | null;
  loading: boolean;
  loadError: string | null;
  running: { controller: AbortController; requestId: string; startedAt: number; progress: CallProgress } | null;
  runError: string | null;
}

const EMPTY: KnowledgeEntry = { status: null, loading: false, loadError: null, running: null, runError: null };

const entries = new Map<string, KnowledgeEntry>();
const listeners = new Set<() => void>();

export function knowledgeKey(context: Pick<RequestContext, 'databaseId' | 'catalog'>): string | null {
  if (context.databaseId === null) return null;
  return `${context.databaseId}|${context.catalog ?? ''}`;
}

function update(key: string, change: (entry: KnowledgeEntry) => KnowledgeEntry): void {
  entries.set(key, change(entries.get(key) ?? EMPTY));
  listeners.forEach(listener => listener());
}

/** Asks the backend what it knows about `context`'s database (again). */
export async function refreshKnowledge(context: RequestContext): Promise<void> {
  const key = knowledgeKey(context);
  if (!key) return;
  update(key, entry => ({ ...entry, loading: true, loadError: null }));
  try {
    const status = await fetchDbKnowledge(context);
    update(key, entry => ({ ...entry, status, loading: false }));
  } catch (e) {
    update(key, entry => ({ ...entry, loading: false, loadError: e instanceof Error ? e.message : 'Could not check' }));
  }
}

let counter = 0;

/** Runs "Interrogate DB" for `context`'s database, its progress landing in that database's entry. */
export async function startInterrogation(context: RequestContext): Promise<void> {
  const key = knowledgeKey(context);
  if (!key || entries.get(key)?.running) return;
  counter += 1;
  const requestId = `interrogate-${Date.now().toString(36)}-${counter}`;
  const controller = new AbortController();
  update(key, entry => ({
    ...entry,
    runError: null,
    running: { controller, requestId, startedAt: Date.now(), progress: EMPTY_PROGRESS },
  }));
  const sink = perFrame<CallProgress>(progress =>
    update(key, entry => (entry.running ? { ...entry, running: { ...entry.running, progress } } : entry)),
  );
  try {
    const summary = await interrogateDb(context, requestId, sink, controller.signal);
    update(key, entry => ({ ...entry, status: { status: 'ready', ...summary } }));
  } catch (e) {
    if (!isCancellation(e)) {
      update(key, entry => ({ ...entry, runError: e instanceof Error ? e.message : 'The interrogation failed' }));
    }
  } finally {
    update(key, entry => ({ ...entry, running: null }));
  }
}

/** Stops the interrogation running for `context`'s database, if one is. */
export function stopInterrogation(context: RequestContext): void {
  const key = knowledgeKey(context);
  const running = key ? entries.get(key)?.running : null;
  if (!running) return;
  running.controller.abort();
  cancelRequest(running.requestId).catch(() => {});
}

export async function forgetKnowledge(context: RequestContext): Promise<void> {
  const key = knowledgeKey(context);
  if (!key) return;
  try {
    await forgetDbKnowledge(context);
  } finally {
    await refreshKnowledge(context);
  }
}

/** The entry for `context`'s database, re-rendering as it changes; fetched the first time it's asked for. */
export function useSchemaKnowledge(context: RequestContext | null): KnowledgeEntry {
  const key = context ? knowledgeKey(context) : null;
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion(version => version + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  useEffect(() => {
    if (!context || !key) return;
    const entry = entries.get(key);
    if (!entry || (!entry.status && !entry.loading && !entry.loadError)) refreshKnowledge(context);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return (key && entries.get(key)) || EMPTY;
}
