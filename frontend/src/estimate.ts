import { useEffect, useState } from 'react';
import { fetchQueryPlan, RequestContext } from './api';
import type { PlanNode, PlanSummary } from './performanceResult';

/**
 * The Query Estimator's arithmetic: how heavy a plan looks, and how to present the backend's plan
 * warnings. Everything here is the planner's *estimate* - the query is never run (see the backend's
 * `query_plan.py`) - so the levels are deliberately coarse.
 */

export type ImpactLevel = 'Low' | 'Moderate' | 'High' | 'Critical' | 'Unknown';

export interface Impact {
  level: ImpactLevel;
  /** How full the bar is drawn, 0-1. */
  fill: number;
  reason: string;
}

/** Thresholds in Postgres' cost units (roughly, sequential page reads). */
const COST_LEVELS: [number, ImpactLevel, number][] = [
  [10_000, 'Low', 0.2],
  [1_000_000, 'Moderate', 0.45],
  [100_000_000, 'High', 0.75],
];

const RANK: Record<ImpactLevel, number> = { Unknown: -1, Low: 0, Moderate: 1, High: 2, Critical: 3 };

export function impactOf(plan: PlanSummary): Impact {
  const warnings = plan.warnings.filter(warning => warning.severity === 'warning');
  let level: ImpactLevel = 'Unknown';
  let fill = 0;
  let reason = 'The database gave no cost estimate.';
  if (plan.totalCost !== null && plan.totalCost !== undefined) {
    const cost = plan.totalCost;
    const band = COST_LEVELS.find(([limit]) => cost < limit);
    [level, fill] = band ? [band[1], band[2]] : ['Critical', 1];
    reason = `Estimated cost ${compactNumber(cost)} ${plan.costUnit}.`;
  } else if (plan.tree || plan.steps) {
    level = warnings.length ? 'High' : plan.warnings.length ? 'Moderate' : 'Low';
    fill = { Low: 0.2, Moderate: 0.45, High: 0.75, Critical: 1, Unknown: 0 }[level];
    reason = 'Judged from the plan warnings - this database gives no cost estimate.';
  }
  // A cartesian product is never "low", whatever the planner thinks of the tables' sizes.
  if (plan.warnings.some(warning => warning.code === 'possible_cross_join') && RANK[level] < RANK.High) {
    level = 'High';
    fill = 0.75;
    reason += ' Raised for a likely cartesian product.';
  }
  return { level, fill, reason };
}

export interface WarningPresentation {
  title: string;
  suggestion: string;
}

const WARNINGS: Record<string, WarningPresentation> = {
  possible_cross_join: { title: 'Cartesian product', suggestion: 'Add JOIN conditions to avoid a cartesian product.' },
  very_high_cost: {
    title: 'Very high cost query',
    suggestion: 'Review the query plan for optimisation opportunities - or try Accelerate in the assistant panel.',
  },
  large_seq_scan: {
    title: 'Large full table scan',
    suggestion: 'Filter on an indexed column, or index the columns it filters on.',
  },
  nested_loop_seq_scan: {
    title: 'Table rescanned in a loop',
    suggestion: 'An index on the inner table’s join column lets each lookup use it instead of a scan.',
  },
  large_sort: { title: 'Large sort', suggestion: 'Sort fewer rows: filter or aggregate first, or LIMIT the result.' },
  full_scan: { title: 'Full table scan', suggestion: 'An index on the filtered or joined columns could avoid it.' },
};

export function presentWarning(code: string): WarningPresentation {
  return (
    WARNINGS[code] ?? {
      title: code.replace(/_/g, ' ').replace(/^\w/, letter => letter.toUpperCase()),
      suggestion: 'See the query plan.',
    }
  );
}

export function compactNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(2);
}

/** Every node of `tree`, depth first. */
export function walk(tree: PlanNode): PlanNode[] {
  return [tree, ...tree.children.flatMap(walk)];
}

// --- One estimate per SQL Lab tab -----------------------------------------------------------------

export interface Estimate {
  sql: string;
  databaseId: number | null;
  plan: PlanSummary | null;
  error: string | null;
  /** Whether the database looked at the query (so `error` is about the SQL) or it was never asked. */
  checked: boolean;
  at: number;
}

export interface EstimateEntry {
  estimate: Estimate | null;
  loading: boolean;
}

const estimates = new Map<string, EstimateEntry>();
const listeners = new Set<() => void>();

function set(tabId: string, entry: EstimateEntry): void {
  estimates.set(tabId, entry);
  listeners.forEach(listener => listener());
}

/** Plans `sql` for `context`'s tab - never runs it - and keeps the answer for that tab. */
export async function estimate(sql: string, context: RequestContext): Promise<void> {
  const previous = estimates.get(context.tabId);
  if (previous?.loading) return;
  set(context.tabId, { estimate: previous?.estimate ?? null, loading: true });
  let next: Estimate;
  try {
    const result = await fetchQueryPlan(sql, context, true);
    next = {
      sql,
      databaseId: context.databaseId,
      plan: result.plan ?? null,
      error: result.plan ? null : result.error ?? 'The database returned no plan.',
      checked: Boolean(result.checked),
      at: Date.now(),
    };
  } catch (e) {
    next = { sql, databaseId: context.databaseId, plan: null, error: e instanceof Error ? e.message : 'Could not plan it', checked: false, at: Date.now() };
  }
  set(context.tabId, { estimate: next, loading: false });
}

export function useEstimate(tabId: string): EstimateEntry {
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion(version => version + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return estimates.get(tabId) ?? { estimate: null, loading: false };
}

/** True while `element` is on screen - SQL Lab renders every tab under the editor all the time,
 * hidden or not, and an estimate costs a planner call (and a line in the user's query history). */
export function useVisible(element: HTMLElement | null): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!element || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)));
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return visible;
}

/**
 * The height `element` can fill: from its top to the bottom of the SQL Lab tab pane it sits in.
 * The host wraps each extension tab in a plain div of no particular height, so `height: 100%`
 * resolves to nothing; this measures instead, and follows the pane as the user drags its divider.
 */
export function useFillHeight(element: HTMLElement | null, minimum = 240): number | undefined {
  const [height, setHeight] = useState<number | undefined>(undefined);
  useEffect(() => {
    if (!element) return undefined;
    const pane = element.closest('.ant-tabs-tabpane') ?? element.closest('.SouthPane');
    if (!pane) return undefined;
    const measure = () => {
      if (element.offsetParent === null) return; // hidden - keep the last height
      const available = pane.getBoundingClientRect().bottom - element.getBoundingClientRect().top - 4;
      setHeight(Math.max(minimum, Math.floor(available)));
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(pane);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [element, minimum]);
  return height;
}
