import React, { useEffect, useState } from 'react';
import { Flex } from 'antd';
import { components, sqlLab } from '@apache-superset/core';
import {
  accelerateSql,
  explainPlan,
  explainSqlError,
  fetchDatabaseBackend,
  generateNl,
  generateSql,
  isCancellation,
  refineSql,
  RequestContext,
  updateNlFromSqlDiff,
  updateSqlFromNl,
} from './api';
import { CallProgress } from './progress';
import {
  contextFor,
  makeRequestId,
  progressSink,
  useActiveSqlLabTab,
  useCurrentTabContext,
  useTabEditor,
} from './sqlLabHooks';
import type { SqlGenerationResult } from './sqlGenerationResult';
import {
  disposeTabState,
  EditEntry,
  GenerationDisplay,
  GroundedPair,
  InFlightCall,
  isSqlEmpty,
  breakdownFor,
  markUnread,
  pushEdit,
  recordManualEditIfAny,
  resetTabState,
  segmentForCallKind,
  segmentForEditType,
  showSegment,
  startCall,
  stopCall,
  TabState,
  updateTabState,
  useTabState,
  WorkflowSegment,
  settleCall,
} from './tabState';
import type { PendingClarifications } from './tabState';
import { useQueryFailureErrors, useValidatorErrors } from './sqlValidation';
import { breakdownSource, requestBreakdown, runBreakdown } from './breakdownAction';
import type { AccelerateResult, BreakdownSection } from './performanceResult';
import { AccelerateCard, AcceleratedResult, fasterSqlOf } from './components/AccelerateCard';
import type { SqlFixResult } from './sqlFixResult';
import { AskComposer } from './components/AskComposer';
import type { ContextBarProps } from './components/ContextBar';
import { DescribeWorkflow } from './components/DescribeWorkflow';
import { EmptyWorkflow } from './components/EmptyWorkflow';
import { FixErrorsCard } from './components/FixErrorsCard';
import { GenerationResult } from './components/GenerationResult';
import { PerformanceActions } from './components/PerformanceActions';
import { PlanExplanationCard } from './components/PlanExplanationCard';
import { Reveal, justLanded } from './components/Reveal';
import { ProgressPanel, useElapsedMs } from './components/ProgressPanel';
import { freshnessOf, Refreshable, useLineChanges } from './components/StaleNotice';
import { QuestionCard } from './components/QuestionCard';
import { RefineComposer } from './components/RefineComposer';
import { ResultActions } from './components/ResultSections';
import { ResponsibilityNote, SafetyNote } from './components/SafetyNote';
import { SchemaKnowledgeBar } from './components/SchemaKnowledgeBar';
import { useSchemaKnowledge } from './schemaKnowledge';
import type { ConversationView, PanelModel, WorkflowView } from './layouts/layout';
import { useWording } from './verbosity';
import { getAncestry, recordChange } from './ancestry';
import { useTokens } from './components/primitives';
import { plainText } from './components/Prose';

function displayAt(history: EditEntry[], position: number): GenerationDisplay | null {
  const entry = history[position - 1];
  return entry ? { type: entry.type, command: entry.command, result: entry.result } : null;
}

/**
 * Whether `targetEditor`'s current content differs from `sqlAtStart` - i.e. whether the user has
 * hand-edited this tab's SQL while a call against it was in flight, which should stop the result
 * from overwriting their edit. Answers "no drift" both when there's no editor to check and when
 * reading one throws: the host keeps a live editor handle only for the tab actually on screen, so a
 * call finishing after the user has switched to another tab hits exactly this - and nobody can have
 * hand-edited a tab's SQL through a screen that isn't showing it. (From sql-helper.)
 */
function hasDrifted(targetEditor: sqlLab.Editor | null, sqlAtStart: string): boolean {
  if (targetEditor === null) return false;
  try {
    return targetEditor.getValue() !== sqlAtStart;
  } catch {
    return false;
  }
}

/**
 * Keeps tab state in sync with SQL Lab tab lifecycle events that `useActiveSqlLabTab` alone
 * doesn't cover - a brand new tab starts from a clean slate (`resetTabState`) rather than
 * whatever a reused id happened to leave behind, and a closed tab's state (and any call still in
 * flight for it) has nothing left to be shown for (`disposeTabState`). Runs once for the whole
 * panel instance, independent of which tab happens to be active.
 */
function useSqlLabTabLifecycle(): void {
  useEffect(() => {
    const createdDisposable = sqlLab.onDidCreateTab(newTab => resetTabState(newTab.id));
    const closedDisposable = sqlLab.onDidCloseTab(closedTab => disposeTabState(closedTab.id));
    return () => {
      createdDisposable.dispose();
      closedDisposable.dispose();
    };
  }, []);
}

const intoSqlCall = (state: TabState, progress: CallProgress): TabState =>
  state.sqlCall ? { ...state, sqlCall: { ...state.sqlCall, progress } } : state;

const intoNlCall = (state: TabState, progress: CallProgress): TabState =>
  state.nlCall ? { ...state, nlCall: { ...state.nlCall, progress } } : state;

const intoFixCall = (state: TabState, progress: CallProgress): TabState =>
  state.fix?.call ? { ...state, fix: { ...state.fix, call: { ...state.fix.call, progress } } } : state;

const intoPerfCall = (state: TabState, progress: CallProgress): TabState =>
  state.perfCall ? { ...state, perfCall: { ...state.perfCall, progress } } : state;

/** What each workflow is called, and what it does - for a layout's tabs, headings and tooltips. */
export const WORKFLOW_LABELS: Record<WorkflowSegment, { label: string; hint: string }> = {
  ask: { label: 'Ask', hint: 'Turn a plain-English question into SQL' },
  refine: { label: 'Refine', hint: 'Change the SQL in the editor by instruction' },
  fix: { label: 'Fix', hint: "Explain and correct the errors on the editor's SQL" },
  describe: { label: 'Describe', hint: "Read the editor's SQL back as a question" },
};

/** What the panel's own header and database bar show - there whether or not a tab is open. */
export interface PanelChrome {
  context: ContextBarProps;
}

/**
 * Everything the side panel does, for whichever SQL Lab tab is active, worked out and handed back
 * as a `PanelModel` for the chosen layout to arrange (see `layouts/layout.ts`) - `model` is null
 * while no tab is open. Each workflow comes back drawn: what differs between layouts is only where
 * the workflows go and which are on screen, never what they do.
 *
 * Kept as one hook rather than one per workflow because the workflows share so much - the undo
 * timeline, the in-flight calls (one running holds back the rest: `busy`), the question that
 * Describe fills in and Ask answers - and because one workflow's results can be shown by another
 * (see `segmentForEditType`).
 */
export function usePanelModel(): { chrome: PanelChrome; model: PanelModel | null } {

  useSqlLabTabLifecycle();
  useQueryFailureErrors();
  const activeTab = useActiveSqlLabTab();
  const { editor, sql, resync } = useTabEditor(activeTab);
  const tabId = activeTab?.id ?? '';
  const tabContext = useCurrentTabContext(activeTab);
  const token = useTokens();
  const say = useWording();
  const [copied, setCopied] = useState(false);
  const hasDatabase = activeTab?.databaseId !== undefined && activeTab?.databaseId !== null;
  const knowledge = useSchemaKnowledge(activeTab && hasDatabase ? contextFor(activeTab) : null);
  // With no database information, SQL written from words can only guess at table names.
  const askHidden = knowledge.status?.status === 'off';

  const [state, updateState] = useTabState(tabId);
  const {
    nlText,
    refineCommand,
    grounded,
    generatingSql,
    generatingNl,
    sqlCall,
    lastSqlCall,
    nlCall,
    perfCall,
    lastPerfCall,
    lastFix,
    fix,
    segment,
    unread,
    description,
    accelerate,
    planExplanation,
    refineView,
    accelerateRunPlan,
    error,
    editHistory,
    editPosition,
    generationDisplay,
    pendingClarifications,
    pendingSql,
    specificAnswers,
    generalAnswers,
    editingQuestion,
  } = state;

  // The breakdown made from the editor's SQL as it is now, if this tab has one; else the latest.
  const breakdown = breakdownFor(state, breakdownSource(sql));
  const breakdownChanges = useLineChanges(breakdown?.sql ?? null, breakdownSource(sql));

  const sqlElapsedMs = useElapsedMs(sqlCall?.startedAt ?? null);
  const nlElapsedMs = useElapsedMs(nlCall?.startedAt ?? null);
  const perfElapsedMs = useElapsedMs(perfCall?.startedAt ?? null);
  const fixElapsedMs = useElapsedMs(fix?.call?.startedAt ?? null);
  // Asked on the same debounce SQL Lab's own editor uses, so the errors this panel talks about are
  // the ones marked in the gutter beside it. Paused while an explanation is being written, and
  // until the editor has resolved: before that `sql` is "" because nothing has been read yet, not
  // because the tab is empty, and acting on that would throw away a tab's errors on every switch.
  useValidatorErrors(activeTab, sql, Boolean(fix?.call) || editor === null);

  // Writes whatever SQL a call left waiting for this tab into its real editor, once that editor
  // exists. Nothing that finishes after an `await` calls `editor.setValue()` directly, because by
  // then the target tab may not be the one on screen and its editor handle may be gone (or, worse,
  // the handle in hand may now be some other tab's). This effect is what actually applies it, as
  // soon as it's this tab's turn: on the same render if the editor is already there, or the moment
  // the user comes back to the tab, however much later that is. (From sql-helper.)
  //
  // Only written when the editor doesn't already hold it: this can run twice for one write (seen
  // in Superset 6.1's dev build), and a second `setValue` in quick succession - a remove then an
  // insert, each reported to SQL Lab's store - can leave the store holding the empty text between
  // the two, which the host's editor component then puts back, wiping the SQL just written.
  useEffect(() => {
    if (!editor || pendingSql === null) return;
    let current: string | null = null;
    try {
      current = editor.getValue();
    } catch {
      current = null;
    }
    if (current !== pendingSql) editor.setValue(pendingSql);
    resync();
    updateState(prev => (prev.pendingSql === pendingSql ? { ...prev, pendingSql: null } : prev));
  }, [editor, pendingSql, updateState, resync]);

  const clarificationsComplete = (
    result: SqlGenerationResult,
    specific: Record<number, string>,
    general: Record<number, string>,
  ): boolean => {
    // Enough to go on: an option picked for every multiple-choice question, or anything typed in
    // a free-text box - the model often asks the same thing both ways, so neither may be made to
    // wait on the other.
    const picked = result.specificClarificationsNeeded.length > 0 && result.specificClarificationsNeeded.every((_, index) => specific[index] !== undefined);
    const typed = result.generalClarificationsNeeded.some((_, index) => (general[index] ?? '').trim() !== '');
    return picked || typed;
  };

  /** Folds the answers to `result`'s clarifying questions into `originalInput`, for a retry. */
  const buildClarifiedInstruction = (
    originalInput: string,
    result: SqlGenerationResult,
    specific: Record<number, string>,
    general: Record<number, string>,
  ): string => {
    if (result.specificClarificationsNeeded.length === 0 && result.generalClarificationsNeeded.length === 0) {
      return originalInput;
    }
    const lines = [originalInput, '', 'Clarifications:'];
    // Only what was answered - an unanswered question is left to the model to judge.
    result.specificClarificationsNeeded.forEach((clarification, index) => {
      if (specific[index] !== undefined) lines.push(`- ${clarification.question}: ${specific[index]}`);
    });
    result.generalClarificationsNeeded.forEach((question, index) => {
      const answer = (general[index] ?? '').trim();
      if (answer) lines.push(`- ${question}: ${answer}`);
    });
    return lines.join('\n');
  };

  /**
   * Runs a "generate/update SQL" call and applies whichever of `SqlGenerationResult`'s three
   * shapes came back - shared by the "Generate/Update SQL" button, "Regenerate", and a
   * clarifications retry, which differ only in what `nl` is and what they ground against.
   * `targetEditor`/`targetId`/`context` are the acting tab's own, captured by the caller before
   * this crosses an `await` - see `tabState.ts`. The SQL is written through `pendingSql`, never
   * straight into `targetEditor`, which is only read to see whether the user has edited the SQL in
   * the meantime (`hasDrifted`).
   */
  const generateSqlAction = async (
    nl: string,
    sqlAtStart: string,
    groundedAtStart: GroundedPair | null,
    useGround: boolean,
    targetEditor: sqlLab.Editor | null,
    targetId: string,
    context: RequestContext,
  ) => {
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoSqlCall);
    updateTabState(targetId, prev => ({
      ...prev,
      generatingSql: true,
      error: '',
      editingQuestion: false,
      sqlCall: startCall(requestId, controller, useGround ? 'update' : 'generate'),
    }));
    try {
      const result = useGround
        ? await updateSqlFromNl(
            groundedAtStart?.nl ?? '',
            groundedAtStart?.sql ?? '',
            nl,
            context,
            requestId,
            sink,
            controller.signal,
          )
        : await generateSql(nl, context, requestId, sink, controller.signal);
      updateTabState(targetId, prev => ({ ...prev, generationDisplay: { type: 'generate', command: nl, result } }));
      if (result.status === 'Success') {
        updateTabState(targetId, prev => ({ ...prev, pendingClarifications: null }));
        // Only apply if the user hasn't manually edited SQL out from under us while we waited.
        if (!hasDrifted(targetEditor, sqlAtStart)) {
          pushEdit(targetId, { type: 'generate', command: nl, oldSql: sqlAtStart, newSql: result.sqlText, result });
          // An update changes the SQL that was there; SQL written from scratch replaces it, and has
          // nothing to compare with.
          if (useGround) recordChange(targetId, 'Updated', sqlAtStart, result.sqlText, getAncestry(targetId), result.changes);
          updateTabState(targetId, prev => ({
            ...prev,
            pendingSql: result.sqlText,
            grounded: { nl, sql: result.sqlText },
            lastAppliedSql: result.sqlText,
          }));
        }
      } else if (result.status === 'ClarificationsRequired') {
        updateTabState(targetId, prev => ({
          ...prev,
          pendingClarifications: { kind: 'generate', originalInput: nl, groundedAtStart, sqlAtStart, result },
          specificAnswers: {},
          generalAnswers: {},
        }));
        pushEdit(targetId, { type: 'generate', command: nl, oldSql: sqlAtStart, newSql: sqlAtStart, result });
      } else {
        updateTabState(targetId, prev => ({ ...prev, pendingClarifications: null }));
        pushEdit(targetId, { type: 'generate', command: nl, oldSql: sqlAtStart, newSql: sqlAtStart, result });
      }
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({ ...prev, error: e instanceof Error ? e.message : 'Failed to generate SQL' }));
      }
    } finally {
      updateTabState(targetId, prev => ({ ...prev, generatingSql: false, sqlCall: null, lastSqlCall: settleCall(prev.sqlCall, prev.lastSqlCall) }));
      markUnread(targetId, 'ask');
    }
  };

  /**
   * Runs a "refine SQL" call and applies whichever of `SqlGenerationResult`'s three shapes came
   * back - shared by the refine box, a suggested refinement, and a clarifications retry, which
   * differ only in what `command` is and whether the refine box should be cleared afterward.
   */
  const refineSqlAction = async (
    command: string,
    sqlAtStart: string,
    clearCommandInput: boolean,
    targetId: string,
    context: RequestContext,
  ) => {
    recordManualEditIfAny(targetId, sqlAtStart);
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoSqlCall);
    updateTabState(targetId, prev => ({
      ...prev,
      generatingSql: true,
      error: '',
      editingQuestion: false,
      refineView: 'refine',
      sqlCall: startCall(requestId, controller, 'refine'),
    }));
    try {
      const result = await refineSql(command, sqlAtStart, context, requestId, sink, controller.signal);
      updateTabState(targetId, prev => ({ ...prev, generationDisplay: { type: 'refine', command, result } }));
      if (result.status === 'Success') {
        pushEdit(targetId, { type: 'refine', command, oldSql: sqlAtStart, newSql: result.sqlText, result });
        recordChange(targetId, 'Refined', sqlAtStart, result.sqlText, getAncestry(targetId), result.changes);
        updateTabState(targetId, prev => ({
          ...prev,
          pendingSql: result.sqlText,
          pendingClarifications: null,
          lastAppliedSql: result.sqlText,
          refineCommand: clearCommandInput ? '' : prev.refineCommand,
        }));
      } else if (result.status === 'ClarificationsRequired') {
        updateTabState(targetId, prev => ({
          ...prev,
          pendingClarifications: { kind: 'refine', originalInput: command, groundedAtStart: null, sqlAtStart, result },
          specificAnswers: {},
          generalAnswers: {},
        }));
        pushEdit(targetId, { type: 'refine', command, oldSql: sqlAtStart, newSql: sqlAtStart, result });
      } else {
        updateTabState(targetId, prev => ({ ...prev, pendingClarifications: null }));
        pushEdit(targetId, { type: 'refine', command, oldSql: sqlAtStart, newSql: sqlAtStart, result });
      }
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({ ...prev, error: e instanceof Error ? e.message : 'Failed to refine SQL' }));
      }
    } finally {
      updateTabState(targetId, prev => ({ ...prev, generatingSql: false, sqlCall: null, lastSqlCall: settleCall(prev.sqlCall, prev.lastSqlCall) }));
      markUnread(targetId, 'refine');
    }
  };

  const handleGenerateSql = () => {
    const nl = nlText.trim();
    if (!nl || generatingSql || !editor || !activeTab) return;
    generateSqlAction(nl, sql, grounded, grounded !== null, editor, activeTab.id, contextFor(activeTab));
  };

  /** "Regenerate": draft the same question again from scratch, rather than editing what the last
   * attempt produced - the point of asking again is usually that that attempt was wrong. */
  const handleRegenerate = () => {
    const nl = nlText.trim();
    if (!nl || generatingSql || !editor || !activeTab) return;
    generateSqlAction(nl, sql, null, false, editor, activeTab.id, contextFor(activeTab));
  };

  const handleUpdateNl = async () => {
    const sqlAtStart = sql.trim();
    if (!sqlAtStart || generatingNl || !editor || !activeTab) return;
    const nlAtStart = nlText;
    const groundedAtStart = grounded;
    const targetId = activeTab.id;
    const context = contextFor(activeTab);
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoNlCall);
    updateTabState(targetId, prev => ({
      ...prev,
      generatingNl: true,
      error: '',
      nlCall: startCall(requestId, controller, 'describe'),
    }));
    try {
      const { text: nl, trace } = groundedAtStart
        ? await updateNlFromSqlDiff(
            groundedAtStart.nl,
            groundedAtStart.sql,
            sqlAtStart,
            context,
            requestId,
            sink,
            controller.signal,
          )
        : await generateNl(sqlAtStart, context, requestId, sink, controller.signal);
      // The description is this workflow's own result, kept on its own tab whatever happens next.
      updateTabState(targetId, prev => ({ ...prev, description: { text: nl, sql: sqlAtStart, trace } }));
      // It is also the ask workflow's input, so it goes into the question box and grounds it - but
      // only if the user hasn't edited that box out from under us while we waited.
      updateTabState(targetId, prev =>
        prev.nlText === nlAtStart ? { ...prev, nlText: nl, grounded: { nl, sql: sqlAtStart } } : prev,
      );
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({
          ...prev,
          error: e instanceof Error ? e.message : 'Failed to generate description',
        }));
      }
    } finally {
      updateTabState(targetId, prev => ({ ...prev, generatingNl: false, nlCall: null }));
      markUnread(targetId, 'describe');
    }
  };

  /** "Break it down": the section-by-section description, drawn beside the SQL it describes (see
   * `SqlBreakdown`). Shares the describe workflow's call slot - it's the same tab. */
  const handleBreakdown = () => {
    if (!activeTab) return;
    runBreakdown(activeTab, sql, false, generatingNl);
  };

  /** Selects a breakdown section's lines in the real editor (and scrolls to them) - the way back
   * from a description to the SQL it is about. */
  const handleSelectSection = (section: BreakdownSection) => {
    if (!editor) return;
    const lines = sql.split('\n');
    const endLine = Math.min(section.endLine, lines.length);
    try {
      editor.setSelection({
        start: { line: section.startLine - 1, column: 0 },
        end: { line: endLine - 1, column: lines[endLine - 1]?.length ?? 0 },
      });
      editor.scrollToLine(section.startLine - 1);
      editor.focus();
    } catch {
      // An editor that can't select (or has gone away mid-click) just doesn't - nothing to report.
    }
  };

  /**
   * Accelerate: asks for a faster version of the editor's SQL (see `performance.py`), shown in the
   * Refine tab as an `AccelerateCard` - nothing changes in the editor until it is applied. With
   * `apply` (Alpha), a faster version that passed its checks (`fasterSqlOf`) goes into the editor as
   * soon as it lands, as a step in the undo timeline - the way Ask's SQL does - unless the checks
   * found it may return different results (MayDiffer): that one waits for `handleApplyAcceleratedHere`.
   */
  const handleAccelerate = async (apply = false) => {
    const sqlAtStart = sql.trim();
    if (isSqlEmpty(sqlAtStart) || perfCall || !activeTab) return;
    const targetId = activeTab.id;
    const editorSqlAtStart = sql;
    if (apply) recordManualEditIfAny(targetId, editorSqlAtStart);
    const context = contextFor(activeTab);
    const runPlan = accelerateRunPlan && context.databaseId !== null;
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoPerfCall);
    updateTabState(targetId, prev => ({
      ...prev,
      error: '',
      refineView: 'accelerate',
      perfCall: startCall(requestId, controller, 'accelerate'),
    }));
    try {
      const result = await accelerateSql(sqlAtStart, runPlan, context, requestId, sink, controller.signal);
      updateTabState(targetId, prev => ({ ...prev, accelerate: { result, sql: sqlAtStart }, refineView: 'accelerate' }));
      const faster = apply && result.check.verdict !== 'MayDiffer' ? fasterSqlOf(result) : null;
      if (faster) putFasterInEditor(targetId, editorSqlAtStart, sqlAtStart, faster, result.changes);
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({ ...prev, error: e instanceof Error ? e.message : 'Failed to accelerate the query' }));
      }
    } finally {
      updateTabState(targetId, prev => ({ ...prev, perfCall: null, lastPerfCall: settleCall(prev.perfCall, prev.lastPerfCall) }));
      markUnread(targetId, 'refine');
    }
  };

  /** Alpha's way of applying a faster version: into the editor as a step in the undo timeline,
   * leaving the tab's view as it is. `fromSql` is what it was rewritten from. */
  const putFasterInEditor = (
    targetId: string,
    editorSqlAtStart: string,
    fromSql: string,
    faster: string,
    changes: AccelerateResult['changes'],
  ) => {
    pushEdit(targetId, { type: 'accelerate', command: null, oldSql: editorSqlAtStart, newSql: faster, result: null });
    updateTabState(targetId, prev => ({ ...prev, pendingSql: faster, lastAppliedSql: faster }));
    recordChange(targetId, 'Accelerated', fromSql, faster, getAncestry(targetId), changes);
  };

  /** Alpha's "Apply" for a faster version the checks said may return different results - the one
   * `handleAccelerate` doesn't put in the editor by itself. */
  const handleApplyAcceleratedHere = () => {
    if (!editor || !activeTab || !accelerate) return;
    const faster = fasterSqlOf(accelerate.result);
    if (!faster) return;
    const targetId = activeTab.id;
    const editorSqlAtStart = editor.getValue();
    recordManualEditIfAny(targetId, editorSqlAtStart);
    putFasterInEditor(targetId, editorSqlAtStart, accelerate.sql, faster, accelerate.result.changes);
  };

  /** "Explain the query plan": the database plans the editor's SQL and the plan is read back. */
  const handleExplainPlan = async () => {
    const sqlAtStart = sql.trim();
    if (isSqlEmpty(sqlAtStart) || perfCall || !activeTab) return;
    const targetId = activeTab.id;
    const context = contextFor(activeTab);
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoPerfCall);
    updateTabState(targetId, prev => ({
      ...prev,
      error: '',
      refineView: 'explain-plan',
      perfCall: startCall(requestId, controller, 'explain-plan'),
    }));
    try {
      const result = await explainPlan(sqlAtStart, context, requestId, sink, controller.signal);
      updateTabState(targetId, prev => ({ ...prev, planExplanation: { result, sql: sqlAtStart }, refineView: 'explain-plan' }));
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({ ...prev, error: e instanceof Error ? e.message : 'Failed to explain the plan' }));
      }
    } finally {
      updateTabState(targetId, prev => ({ ...prev, perfCall: null, lastPerfCall: settleCall(prev.perfCall, prev.lastPerfCall) }));
      markUnread(targetId, 'refine');
    }
  };

  /** Puts the faster version in this tab's editor, as its own step in the undo timeline. */
  const handleApplyAccelerate = () => {
    if (!editor || !activeTab || !accelerate) return;
    const targetId = activeTab.id;
    const sqlAtStart = editor.getValue();
    const fasterSql = accelerate.result.rewrittenSql;
    recordManualEditIfAny(targetId, sqlAtStart);
    pushEdit(targetId, { type: 'accelerate', command: null, oldSql: sqlAtStart, newSql: fasterSql, result: null });
    updateTabState(targetId, prev => ({
      ...prev,
      pendingSql: fasterSql,
      lastAppliedSql: fasterSql,
      generationDisplay: { type: 'accelerate', command: null, result: null },
      refineView: 'refine',
    }));
    // What it was rewritten from - the SQL the faster version is meant to be equivalent to - for
    // the "Compare with original" tab under the editor.
    recordChange(targetId, 'Accelerated', accelerate.sql, fasterSql, getAncestry(targetId), accelerate.result.changes);
  };

  /** Opens the faster version in a new SQL Lab tab on the same database, leaving this one as it was
   * to compare against - waxwing-sql's way of doing it. */
  const handleOpenAccelerateInNewTab = async () => {
    if (!activeTab || !accelerate) return;
    const { sql: fromSql, result } = accelerate;
    const base = getAncestry(activeTab.id);
    try {
      const created = await sqlLab.createTab({
        sql: result.rewrittenSql,
        title: `Faster: ${activeTab.title}`,
        databaseId: activeTab.databaseId,
        catalog: activeTab.catalog,
        schema: activeTab.schema,
      });
      if (created?.id) recordChange(created.id, 'Accelerated', fromSql, result.rewrittenSql, base, result.changes);
    } catch (e) {
      updateState(prev => ({ ...prev, error: e instanceof Error ? e.message : 'Could not open a new tab' }));
    }
  };

  const handleRefineSql = () => {
    const command = refineCommand.trim();
    if (!command || isSqlEmpty(sql) || generatingSql || !editor || !activeTab) return;
    refineSqlAction(command, sql, true, activeTab.id, contextFor(activeTab));
  };

  const handleRetryWithClarifications = () => {
    const pending = pendingClarifications;
    if (!pending || !editor || !activeTab || !clarificationsComplete(pending.result, specificAnswers, generalAnswers)) return;
    retryClarified(pending, buildClarifiedInstruction(pending.originalInput, pending.result, specificAnswers, generalAnswers));
  };

  /** An option clicked in the conversation's clarifying questions (`ClarificationPrompts`): it's
   * picked, and once every question has one the answers are sent together - so asking several
   * questions doesn't send the first answer alone. */
  const pickClarification = (index: number, option: string) => {
    const pending = pendingClarifications;
    if (!pending || busy) return;
    const picked = { ...specificAnswers, [index]: option };
    updateState(prev => ({ ...prev, specificAnswers: { ...prev.specificAnswers, [index]: option } }));
    if (pending.result.specificClarificationsNeeded.every((_, i) => picked[i] !== undefined)) {
      retryClarified(pending, buildClarifiedInstruction(pending.originalInput, pending.result, picked, {}));
    }
  };

  /** Asks the paused question again with its answers folded in, grounded as it first was. */
  const retryClarified = (pending: PendingClarifications, instruction: string) => {
    if (!editor || !activeTab) return;
    if (pending.kind === 'generate') {
      generateSqlAction(
        instruction,
        pending.sqlAtStart,
        pending.groundedAtStart,
        pending.groundedAtStart !== null,
        editor,
        activeTab.id,
        contextFor(activeTab),
      );
    } else {
      refineSqlAction(instruction, pending.sqlAtStart, false, activeTab.id, contextFor(activeTab));
    }
  };

  /** Drops the clarifying questions unanswered and hands the question back for another go -
   * nothing was written into the editor for them, so there's nothing to undo here. */
  const handleCancelClarifications = () =>
    updateState(prev => ({ ...prev, pendingClarifications: null, generationDisplay: null, editingQuestion: true }));

  /** A "suggestionsForRefinement" button: immediately runs that suggestion as a refinement. */
  const handleSuggestionSelected = (offered: string) => {
    if (isSqlEmpty(sql) || !editor || !activeTab) return;
    const suggestion = plainText(offered);
    updateState(prev => ({ ...prev, refineCommand: suggestion }));
    // Offered on the Ask tab, but it's a refinement - so it runs where refinements are shown,
    // rather than leaving the user watching a tab its result will never appear on.
    showSegment(activeTab.id, 'refine');
    refineSqlAction(suggestion, sql, true, activeTab.id, contextFor(activeTab));
  };

  const busy = generatingSql || generatingNl || perfCall !== null;

  /**
   * Whether the editor's SQL - or the SQL about to be written into it - is one of `candidates`. A
   * result is shown (Alpha) only while this holds for the SQL it was about and the SQL it wrote,
   * so stepping back and forward through it with undo and redo keeps it, and any other change to
   * the SQL - typed, or another workflow's - puts it away rather than leaving it about old SQL.
   */
  const aboutTheEditorsSql = (...candidates: (string | null | undefined)[]) => {
    const now = (pendingSql ?? sql).trim();
    return candidates.some(candidate => candidate != null && candidate.trim() === now);
  };

  /**
   * Asks for the editor's SQL errors to be explained and corrected. Sent against the SQL the errors
   * were actually reported for (`fix.sql`), not whatever is in the editor by the time the button is
   * pressed - an explanation of an error in text that has since been edited would be fiction.
   */
  const handleExplainError = async (apply = false) => {
    if (!fix || fix.call || !activeTab) return;
    const targetId = activeTab.id;
    const editorSqlAtStart = sql;
    if (apply) recordManualEditIfAny(targetId, editorSqlAtStart);
    let landed: SqlFixResult | null = null;
    const context = contextFor(activeTab);
    const { sql: sqlAtStart, errors } = fix;
    const controller = new AbortController();
    const requestId = makeRequestId();
    const sink = progressSink(targetId, intoFixCall);
    const databaseId = activeTab.databaseId;
    updateTabState(targetId, prev =>
      prev.fix
        ? { ...prev, error: '', fix: { ...prev.fix, result: null, call: startCall(requestId, controller, 'fix') } }
        : prev,
    );
    try {
      // Which engine's error message this is makes a real difference to explaining it, but it's not
      // worth blocking the call on: an unreadable database resolves to no dialect, not a failure.
      const dialect = databaseId === undefined ? '' : await fetchDatabaseBackend(databaseId);
      const result = await explainSqlError(sqlAtStart, errors, dialect, context, requestId, sink, controller.signal);
      updateTabState(targetId, prev => (prev.fix ? { ...prev, fix: { ...prev.fix, result } } : prev));
      landed = result;
    } catch (e) {
      if (!isCancellation(e)) {
        updateTabState(targetId, prev => ({
          ...prev,
          error: e instanceof Error ? e.message : 'Failed to explain the SQL error',
        }));
      }
    } finally {
      const result = apply ? landed : null;
      const fixedSql = result && result.status === 'Fixed' && result.fixedSql.trim() !== '' ? result.fixedSql : null;
      if (fixedSql) {
        pushEdit(targetId, { type: 'fix', command: null, oldSql: editorSqlAtStart, newSql: fixedSql, result: null });
        recordChange(targetId, 'Fixed', sqlAtStart, fixedSql, getAncestry(targetId), result?.changes);
      }
      // One update, so nothing is drawn between the call settling and its result being kept.
      updateTabState(targetId, prev => {
        if (!prev.fix) return prev;
        const settled = settleCall(prev.fix.call, prev.fix.lastCall);
        const next = { ...prev, fix: { ...prev.fix, call: null, lastCall: settled } };
        if (!result || !settled) return next;
        const lastFix = { sql: sqlAtStart, errors, call: settled, result, applied: fixedSql !== null };
        // Applied, the errors go with the SQL that had them; anything still wrong comes back from
        // the validator (or the next run) on its own.
        return fixedSql ? { ...next, lastFix, pendingSql: fixedSql, lastAppliedSql: fixedSql, fix: null } : { ...next, lastFix };
      });
      markUnread(targetId, 'fix');
    }
  };

  /** Puts a proposed correction in the editor, as its own step in the undo timeline - so undo puts
   * the query that was failing back, rather than leaving the user with no way to compare them. */
  const handleApplyFix = (fixedSql: string) => {
    if (!editor || !activeTab) return;
    const targetId = activeTab.id;
    const sqlAtStart = editor.getValue();
    recordManualEditIfAny(targetId, sqlAtStart);
    pushEdit(targetId, { type: 'fix', command: null, oldSql: sqlAtStart, newSql: fixedSql, result: null });
    recordChange(targetId, 'Fixed', sqlAtStart, fixedSql, getAncestry(targetId), fix?.result?.changes);
    updateTabState(targetId, prev => ({
      ...prev,
      pendingSql: fixedSql,
      lastAppliedSql: fixedSql,
      generationDisplay: { type: 'fix', command: null, result: null },
      // The errors this was about are gone with the SQL that had them; anything still wrong with it
      // will come back from the validator (or the next run) on its own.
      fix: null,
    }));
  };

  /** Stops only the explanation, leaving any generation running - they're separate workflows, and
   * the Stop inside the error card sits next to the one it belongs to. */
  const handleStopFix = () => stopCall(fix?.call ?? null);

  const handleDismissFix = () =>
    updateState(prev => (prev.fix ? { ...prev, fix: { ...prev.fix, dismissed: true } } : prev));

  /** `inPlace` (Alpha): only the editor steps back - the tab and the response on it stay as they
   * are, rather than moving to the workflow that made the step stepped onto. */
  const handleUndo = (inPlace = false) => {
    if (busy || !editor) return;
    const position = editPosition - 1;
    const entry = editHistory[position];
    if (!entry) return;
    updateState(prev => ({
      ...prev,
      pendingSql: entry.oldSql,
      lastAppliedSql: entry.oldSql,
      editPosition: position,
      generationDisplay: inPlace ? prev.generationDisplay : displayAt(prev.editHistory, position),
      pendingClarifications: inPlace ? prev.pendingClarifications : null,
      refineView: inPlace ? prev.refineView : 'refine',
    }));
    if (!inPlace) followStepToItsWorkflow(displayAt(editHistory, position));
  };

  /** `inPlace`: as for `handleUndo`. */
  const handleRedo = (inPlace = false) => {
    if (busy || !editor) return;
    const entry = editHistory[editPosition];
    if (!entry) return;
    updateState(prev => ({
      ...prev,
      pendingSql: entry.newSql,
      lastAppliedSql: entry.newSql,
      editPosition: editPosition + 1,
      generationDisplay: inPlace ? prev.generationDisplay : displayAt(prev.editHistory, editPosition + 1),
      pendingClarifications: inPlace ? prev.pendingClarifications : null,
      refineView: inPlace ? prev.refineView : 'refine',
    }));
    if (!inPlace) followStepToItsWorkflow(displayAt(editHistory, editPosition + 1));
  };

  /**
   * Shows the workflow that produced the step just stepped onto, since that's the tab its detail
   * renders on (see `segmentForEditType`) - the one case where the panel changes tabs by itself,
   * and it does so because the user pressed a button whose whole purpose is to move between steps.
   */
  const followStepToItsWorkflow = (display: GenerationDisplay | null) => {
    if (!activeTab || !display) return;
    const destination = segmentForEditType(display.type);
    if (destination) showSegment(activeTab.id, destination);
  };

  const handleCopy = () => {
    if (!sql.trim()) return;
    navigator.clipboard?.writeText(sql).then(
      () => setCopied(true),
      () => updateState(prev => ({ ...prev, error: 'Could not copy the SQL to the clipboard' })),
    );
  };

  // Let "Copied" fall back to "Copy" on its own, so the button doesn't sit there claiming
  // something that happened a while ago.
  useEffect(() => {
    if (!copied) return undefined;
    const handle = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(handle);
  }, [copied]);

  /** Hands the description to the ask workflow: into the question box, grounded in the SQL it was
   * written from, so editing it there asks for a change to that query rather than a fresh one. */
  const handleOpenDescriptionInAsk = () => {
    if (!activeTab) return;
    updateState(prev =>
      prev.description
        ? {
            ...prev,
            nlText: plainText(prev.description.text),
            grounded: { nl: plainText(prev.description.text), sql: prev.description.sql },
            editingQuestion: true,
          }
        : prev,
    );
    showSegment(activeTab.id, 'ask');
  };

  // No SQL Lab tab to act on (including the "no tabs open at all" case, since the last tab
  // closing leaves no active tab either): no workflows, but the panel's own chrome is still shown
  // (see `NLSqlHelperPanel`), rather than the panel hiding behind a plain message.
  const chrome: PanelChrome = { context: tabContext };
  if (!activeTab) return { chrome, model: null };

  const hasSql = !isSqlEmpty(sql);
  const noDatabase = activeTab.databaseId === undefined || activeTab.databaseId === null;
  const sqlUnchanged = grounded !== null && sql === grounded.sql;
  const nlUnchanged = grounded !== null && nlText === grounded.nl;
  const canRetryClarifications =
    pendingClarifications !== null && clarificationsComplete(pendingClarifications.result, specificAnswers, generalAnswers);
  const describeLabel = grounded ? 'Update the question from the SQL' : "Describe the editor's SQL";
  const describeDisabled = busy || !editor || !hasSql || sqlUnchanged;
  const problems = fix && !fix.dismissed ? fix.errors.length : 0;

  // One result at a time, shown by the workflow that produced it. A hand edit belongs to none of
  // them; it shows on Refine, which is where the panel sits by default.
  const askDisplay = generationDisplay?.type === 'generate' ? generationDisplay : null;
  const refineDisplay =
    generationDisplay && ['refine', 'manual', 'accelerate'].includes(generationDisplay.type) ? generationDisplay : null;
  const fixDisplay = generationDisplay?.type === 'fix' ? generationDisplay : null;

  // Before the first result there's nothing else the Ask tab could be showing, so the composer is
  // the whole of it; afterwards it's only back on explicit request (see `editingQuestion`).
  const showComposer = editingQuestion || (!askDisplay && !generatingSql);
  const questionCard = nlText.trim() !== '' && (
    <QuestionCard
      question={nlText.trim()}
      onEdit={() => updateState(prev => ({ ...prev, editingQuestion: true }))}
      editDisabled={busy}
    />
  );

  /** The progress display for one call, on the tab that started it, with its own Stop. */
  const progressPanel = (
    call: InFlightCall | null,
    elapsedMs: number,
    detail?: string,
    flowing = false,
    finished = false,
  ) =>
    call && (
      <ProgressPanel
        kind={call.kind}
        detail={detail}
        progress={call.progress}
        elapsedMs={elapsedMs}
        onStop={() => stopCall(call)}
        flowing={flowing}
        finished={finished}
      />
    );

  const undoRedoProps = {
    onUndo: () => handleUndo(),
    onRedo: () => handleRedo(),
    onCopy: handleCopy,
    canUndo: !busy && !!editor && editPosition > 0,
    canRedo: !busy && !!editor && editPosition < editHistory.length,
    canCopy: hasSql,
    copied,
    position: editPosition,
    total: editHistory.length,
  };

  /** Alpha's: undo and redo change only the editor, leaving the tab and its response where they are. */
  const inPlaceUndoRedoProps = { ...undoRedoProps, onUndo: () => handleUndo(true), onRedo: () => handleRedo(true) };

  /** Undo/redo with no result beside them: the timeline has been stepped back before its first
   * step, but isn't empty, so the way forward again mustn't vanish with the result. (From
   * sql-helper.) */
  const bareEditControls = editHistory.length > 0 && (
    <ResultActions
      showRegenerate={false}
      onRegenerate={handleRegenerate}
      canRegenerate={false}
      {...undoRedoProps}
    />
  );

  /** The result of whatever the panel last did, with the actions that act on the SQL now in the
   * editor. Rendered by the workflow that produced it (see `segmentForEditType`), so a result never
   * appears under a tab that had nothing to do with it. */
  const generationResult = (
    display: GenerationDisplay,
    onSuggestion: (offered: string) => void = handleSuggestionSelected,
    conversational = false,
    brief = false,
  ) => (
    <GenerationResult
      display={display}
      specificAnswers={specificAnswers}
      generalAnswers={generalAnswers}
      onSpecificAnswer={(index, optionValue) =>
        conversational
          ? pickClarification(index, optionValue)
          : updateState(prev => ({ ...prev, specificAnswers: { ...prev.specificAnswers, [index]: optionValue } }))
      }
      onGeneralAnswer={(index, textValue) =>
        updateState(prev => ({ ...prev, generalAnswers: { ...prev.generalAnswers, [index]: textValue } }))
      }
      canRetry={canRetryClarifications}
      onRetryWithClarifications={handleRetryWithClarifications}
      onCancelClarifications={handleCancelClarifications}
      onSuggestionSelected={onSuggestion}
      conversational={conversational}
      brief={brief}
      onRephrase={() => updateState(prev => ({ ...prev, editingQuestion: true }))}
      onRegenerate={handleRegenerate}
      canRegenerate={!busy && !!editor && nlText.trim() !== ''}
      busy={busy}
      {...(brief ? inPlaceUndoRedoProps : undoRedoProps)}
    />
  );

  // Whether the assistant knows the tab's database - offered where SQL is written from words.
  const knowledgeBar = askHidden ? null : (
    <SchemaKnowledgeBar context={noDatabase ? null : contextFor(activeTab)} databaseLabel={tabContext.databaseName} />
  );

  const askBody = (
    <>
      {sqlCall && segmentForCallKind(sqlCall.kind) === 'ask' ? (
        <>
          {questionCard}
          {progressPanel(sqlCall, sqlElapsedMs)}
        </>
      ) : (
        <>
          {showComposer ? (
            <AskComposer
              value={nlText}
              onChange={value => updateState(prev => ({ ...prev, nlText: value }))}
              onSubmit={handleGenerateSql}
              submitLabel={grounded ? 'Update SQL' : 'Generate SQL'}
              submitting={generatingSql}
              submitDisabled={busy || !editor || !nlText.trim() || nlUnchanged}
              onCancelEdit={askDisplay ? () => updateState(prev => ({ ...prev, editingQuestion: false })) : null}
            />
          ) : (
            questionCard
          )}
          {!showComposer && askDisplay && generationResult(askDisplay)}
        </>
      )}
    </>
  );
  // Refine: the performance actions first (what the panel can do with nothing but the query), then
  // whatever is running, then the latest of its three kinds of result.
  const refineResult = (() => {
    if (refineView === 'accelerate' && accelerate) {
      return (
        <AccelerateCard
          result={accelerate.result}
          originalSql={accelerate.sql}
          freshness={freshnessOf(accelerate.sql !== sql.trim(), perfCall?.kind === 'accelerate')}
          onApply={handleApplyAccelerate}
          onOpenInNewTab={handleOpenAccelerateInNewTab}
          busy={busy || !editor}
        />
      );
    }
    if (refineView === 'explain-plan' && planExplanation) {
      return (
        <PlanExplanationCard
          result={planExplanation.result}
          freshness={freshnessOf(planExplanation.sql !== sql.trim(), perfCall?.kind === 'explain-plan')}
        />
      );
    }
    if (refineDisplay) return generationResult(refineDisplay);
    if (bareEditControls) return bareEditControls;
    return (
      <EmptyWorkflow
        next={say(
          'Say what to change about it below — “only the top 5”, “add the percentage change” — or press Accelerate for a faster version.',
          null,
        )}
      >
        {say(`The editor holds ${sql.trim().split('\n').length} lines of SQL.`, 'Say what to change below, or press Accelerate.')}
      </EmptyWorkflow>
    );
  })();

  const refineRunning = sqlCall && segmentForCallKind(sqlCall.kind) === 'refine';
  // Whether the result on show is the kind a running performance call will replace.
  const perfResultShowing = (kind: string) =>
    (kind === 'accelerate' && refineView === 'accelerate' && accelerate !== null) ||
    (kind === 'explain-plan' && refineView === 'explain-plan' && planExplanation !== null);
  const refineWithSql = (
    <>
      <PerformanceActions
        onAccelerate={() => handleAccelerate()}
        onExplainPlan={handleExplainPlan}
        runPlan={accelerateRunPlan}
        onRunPlanChange={value => updateState(prev => ({ ...prev, accelerateRunPlan: value }))}
        disabled={busy}
        noDatabase={noDatabase}
        accelerating={perfCall?.kind === 'accelerate'}
        explaining={perfCall?.kind === 'explain-plan'}
      />
      {refineRunning && progressPanel(sqlCall, sqlElapsedMs, `“${refineCommand.trim()}”`)}
      {perfCall && progressPanel(perfCall, perfElapsedMs)}
      {!refineRunning && !perfCall && refineResult}
      {/* Being redone: the result it replaces stays underneath, dimmed, until the new one lands. */}
      {!refineRunning && perfCall && perfResultShowing(perfCall.kind) && <Refreshable refreshing>{refineResult}</Refreshable>}
    </>
  );

  const refineBody = !hasSql ? (
    <EmptyWorkflow
      next={say(
        `Type or paste some SQL in the editor${askHidden ? '' : ' (or ask for some on the Ask tab)'}, and this is where you change it, speed it up, or see how the database would run it.`,
        null,
      )}
    >
      {say('There is no SQL in the editor to refine yet.', askHidden ? 'No SQL to refine yet - write some.' : 'No SQL to refine yet - write some, or use Ask.')}
    </EmptyWorkflow>
  ) : (
    refineWithSql
  );

  /** Alpha's Fix tab, read the same way as Ask: the errors, then the progress - kept once it lands -
   * then the corrected SQL already in the editor (`handleExplainError(true)`), with undo, redo and
   * copy. A fix that has landed (`lastFix`) stays on screen until there's a new set of errors. */
  const fixFlow = () => {
    const live = fix && !fix.dismissed ? fix : null;
    // A fix of some other SQL than the editor's isn't shown (see `aboutTheEditorsSql`).
    const lastFixShown =
      lastFix && aboutTheEditorsSql(lastFix.sql, lastFix.applied && lastFix.result.status === 'Fixed' ? lastFix.result.fixedSql : null)
        ? lastFix
        : null;
    const fresh = live && (live.call || !lastFixShown || live.sql !== lastFixShown.sql) ? live : null;
    if (fresh) {
      return (
        <FixErrorsCard
          errors={fresh.errors}
          progress={fresh.call?.progress ?? null}
          elapsedMs={fixElapsedMs}
          result={null}
          otherRunning={busy}
          onExplain={() => handleExplainError(true)}
          onStop={handleStopFix}
          onApply={handleApplyFix}
          onDismiss={handleDismissFix}
          flowing
        />
      );
    }
    if (!lastFixShown) return null;
    return (
      <FixErrorsCard
        errors={lastFixShown.errors}
        progress={null}
        elapsedMs={0}
        result={lastFixShown.result}
        otherRunning={busy}
        onExplain={() => handleExplainError(true)}
        onStop={handleStopFix}
        onApply={handleApplyFix}
        onDismiss={null}
        settled={lastFixShown.call}
        flowing
        applied={lastFixShown.applied}
        originalSql={lastFixShown.sql}
        actions={inPlaceUndoRedoProps}
      />
    );
  };

  const fixView = (flowing: boolean) =>
    (flowing && fixFlow()) ||
    (fix && !fix.dismissed ? (
      <FixErrorsCard
        errors={fix.errors}
        progress={fix.call?.progress ?? null}
        elapsedMs={fixElapsedMs}
        result={fix.result}
        otherRunning={busy}
        onExplain={() => handleExplainError()}
        onStop={handleStopFix}
        onApply={handleApplyFix}
        onDismiss={handleDismissFix}
      />
    ) : !flowing && fixDisplay ? (
      generationResult(fixDisplay)
    ) : (
      <EmptyWorkflow
        next={say(
          "Errors show up here as you type - from Superset's SQL validator, or from parsing the SQL where the database has none - and as soon as a run comes back rejected.",
          null,
        )}
      >
        {say('Nothing is reported against the SQL in the editor.', "No problems with the editor's SQL.")}
      </EmptyWorkflow>
    ));
  const fixBody = fixView(false);

  const describeBody = (
    <DescribeWorkflow
      actionLabel={describeLabel}
      actionDisabled={describeDisabled}
      onDescribe={handleUpdateNl}
      description={description}
      freshness={freshnessOf(description !== null && description.sql !== sql, nlCall?.kind === 'describe')}
      onOpenInAsk={handleOpenDescriptionInAsk}
      busy={busy}
      onBreakdown={handleBreakdown}
      breakdownDisabled={busy || !hasSql}
      breakdown={breakdown}
      breakdownFreshness={freshnessOf(breakdown !== null && breakdown.sql !== breakdownSource(sql), nlCall?.kind === 'breakdown')}
      breakdownChangedLines={breakdownChanges?.count}
      onSelectInEditor={
        breakdown !== null && !breakdown.result.formatted && breakdown.sql === breakdownSource(sql) && editor
          ? handleSelectSection
          : null
      }
      progress={progressPanel(nlCall, nlElapsedMs)}
    />
  );

  const runningOn = (target: WorkflowSegment) =>
    [sqlCall, nlCall, perfCall].some(call => call !== null && segmentForCallKind(call.kind) === target);
  const fixRunning = fix?.call !== null && fix?.call !== undefined;

  const workflow = (
    id: WorkflowSegment,
    view: Pick<WorkflowView, 'body' | 'available' | 'wantsAttention' | 'running' | 'usesSchema'> & { hidden?: boolean },
  ): WorkflowView => ({
    id,
    ...WORKFLOW_LABELS[id],
    hidden: false,
    ...view,
    unread: unread[id],
    count: id === 'fix' ? problems : 0,
  });

  // Asking as a conversation (see `ConversationView`): the question and follow-up calls, and the
  // latest of them with no question card - the conversation shows what was said itself.
  const conversationCall = sqlCall && ['generate', 'update', 'refine'].includes(sqlCall.kind) ? sqlCall : null;
  const latestAsked = generationDisplay && ['generate', 'refine'].includes(generationDisplay.type) ? generationDisplay : null;
  // Only while the editor holds the SQL it was asked about or the SQL it wrote (see
  // `aboutTheEditorsSql`) - and with it go any questions it was waiting on answers to.
  const latestAskedStep = latestAsked?.result ? editHistory.find(entry => entry.result === latestAsked.result) : undefined;
  const conversationDisplay = latestAsked && (!latestAskedStep || aboutTheEditorsSql(latestAskedStep.oldSql, latestAskedStep.newSql)) ? latestAsked : null;
  /** While the model is waiting on clarifying questions, whatever is typed next is the answer to
   * them, not a new question - sent with any options picked so far. */
  const answerClarifications = (text: string): boolean => {
    const pending = pendingClarifications;
    if (!pending || pending.result.status !== 'ClarificationsRequired' || !conversationDisplay) return false;
    const picked = buildClarifiedInstruction(pending.originalInput, pending.result, specificAnswers, {});
    const base = picked === pending.originalInput ? `${picked}\n\nClarifications:` : picked;
    retryClarified(pending, `${base}\n- ${text.trim()}`);
    return true;
  };
  const conversation: ConversationView = {
    ask: text => {
      if (!text.trim() || busy || !editor) return;
      if (answerClarifications(text)) return;
      updateState(prev => ({ ...prev, nlText: text, grounded: null, editingQuestion: false }));
      generateSqlAction(text.trim(), sql, null, false, editor, activeTab.id, contextFor(activeTab));
    },
    followUp: text => {
      if (!text.trim() || busy || !editor) return;
      if (answerClarifications(text)) return;
      refineSqlAction(text.trim(), sql.trim(), false, activeTab.id, contextFor(activeTab));
    },
    hasSql,
    canSend: !busy && !!editor,
    sending: generatingSql,
    explainQuery: () => {
      if (hasSql && activeTab) requestBreakdown(activeTab.id);
    },
    waitingOn: !busy
      ? null
      : generatingSql
        ? null // its own call - the Stop is on screen
        : nlCall?.kind === 'breakdown'
          ? 'Break it down is still running'
          : perfCall?.kind === 'accelerate'
            ? 'Accelerate is still running'
            : 'Another request is still running',
    latest: onSuggestion =>
      // The progress stays on screen once the call lands, marked done, with the result arriving
      // under it - the same two places either way, so nothing is swapped out or remounted.
      conversationCall || conversationDisplay ? (
        <>
          {conversationCall
            ? progressPanel(conversationCall, sqlElapsedMs, undefined, true)
            : lastSqlCall && progressPanel(lastSqlCall, lastSqlCall.finishedAt - lastSqlCall.startedAt, undefined, true, true)}
          {!conversationCall && conversationDisplay && (
            <Reveal active={justLanded(lastSqlCall?.finishedAt)}>
              {generationResult(conversationDisplay, offered => onSuggestion(plainText(offered)), true, true)}
            </Reveal>
          )}
        </>
      ) : null,
  };

  const accelerating = perfCall?.kind === 'accelerate';
  /** The editor's SQL has errors (the Fix tab's) - Alpha won't speed up a query that doesn't work. */
  const accelerateBlocked = fix !== null && fix.errors.length > 0 && fix.sql.trim() === sql.trim();
  const settledAccelerate = lastPerfCall?.kind === 'accelerate' ? lastPerfCall : null;
  // Only a faster version of the editor's SQL as it is - or the one it put there (see
  // `aboutTheEditorsSql`); for anything else, the tab runs Accelerate afresh when it's opened.
  const accelerateShown = accelerate && aboutTheEditorsSql(accelerate.sql, fasterSqlOf(accelerate.result)) ? accelerate : null;
  // Alpha applies a faster version as soon as it lands - unless the checks found it may return
  // different results - so not the shared "nothing changes until you apply it".
  const accelerateDetail =
    'Looking for a faster query that returns the same result. If the checks find no difference, it goes straight into the editor - Undo takes it back. If they find one, you decide.';
  const accelerateFlow =
    accelerating || accelerateShown ? (
      <>
        {accelerating
          ? progressPanel(perfCall, perfElapsedMs, accelerateDetail, true)
          : settledAccelerate &&
            progressPanel(
              settledAccelerate,
              settledAccelerate.finishedAt - settledAccelerate.startedAt,
              accelerateDetail,
              true,
              true,
            )}
        {!accelerating && accelerate && (
          <Reveal active={justLanded(settledAccelerate?.finishedAt)}>
            <AcceleratedResult
              result={accelerate.result}
              originalSql={accelerate.sql}
              applied={aboutTheEditorsSql(fasterSqlOf(accelerate.result))}
              onApply={handleApplyAcceleratedHere}
              busy={busy}
              actions={inPlaceUndoRedoProps}
            />
          </Reveal>
        )}
      </>
    ) : null;

  const model: PanelModel = {
    tabKey: activeTab.id,
    conversation,
    flowing: {
      accelerate: accelerateFlow,
      fix: fixView(true),
      sqlErrors: accelerateBlocked,
      // Only errors on show and not yet looked at - a result already there isn't asked for again.
      runFix: () => {
        if (fix && !fix.dismissed && !fix.call && !fix.result && fix.sql !== lastFix?.sql) handleExplainError(true);
      },
      runAccelerate: () => {
        if (!accelerateBlocked) handleAccelerate(true);
      },
      accelerateStatus: hasSql
        ? {
            disabled: busy,
            running: accelerating,
            // The faster version for this SQL is on show - or it's the SQL now, applied.
            current: accelerate !== null && (accelerate.sql === sql.trim() || fasterSqlOf(accelerate.result)?.trim() === sql.trim()),
          }
        : null,
    },
    workflows: {
      ask: workflow('ask', {
        body: askBody,
        available: !askHidden,
        hidden: askHidden,
        wantsAttention: !hasSql || runningOn('ask') || editingQuestion,
        running: runningOn('ask'),
        usesSchema: true,
      }),
      refine: workflow('refine', {
        body: refineBody,
        available: hasSql,
        wantsAttention: hasSql,
        running: runningOn('refine'),
        usesSchema: true,
      }),
      fix: workflow('fix', {
        body: fixBody,
        available: Boolean((fix && !fix.dismissed) || fixDisplay),
        wantsAttention: problems > 0 || fixRunning,
        running: fixRunning,
        usesSchema: false,
      }),
      describe: workflow('describe', {
        body: describeBody,
        available: hasSql || Boolean(description),
        wantsAttention: runningOn('describe'),
        running: runningOn('describe'),
        usesSchema: false,
      }),
    },
    focused: segment,
    focus: next => showSegment(activeTab.id, next),
    error: error ? (
      <components.Alert type="error" showIcon closable onClose={() => updateState(prev => ({ ...prev, error: '' }))}>
        {error}
      </components.Alert>
    ) : null,
    schemaBar: knowledgeBar,
    footer: withComposer => {
      const composer = hasSql && withComposer;
      return (
        <Flex
          vertical
          gap={token.marginXS}
          style={{ flexShrink: 0, padding: token.paddingSM, borderTop: `1px solid ${token.colorBorderSecondary}` }}
        >
          <SafetyNote refinement={composer} />
          <ResponsibilityNote />
          {composer && (
            <RefineComposer
              value={refineCommand}
              onChange={value => updateState(prev => ({ ...prev, refineCommand: value }))}
              onSubmit={handleRefineSql}
              disabled={busy || !editor}
              submitting={generatingSql}
            />
          )}
        </Flex>
      );
    },
  };
  return { chrome, model };
}
