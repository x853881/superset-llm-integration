import { useEffect } from 'react';
import { sqlLab } from '@apache-superset/core';
import { NoValidatorError, validateSql } from './api';
import { recordQueryFailure, recordValidatorErrors } from './tabState';
import type { SqlError } from './sqlFixResult';

/** How long the SQL has to stop changing before it's sent for validation. Matches SQL Lab's own
 * `VALIDATION_DEBOUNCE_MS`, so this panel asks on the same rhythm the editor's gutter does. */
const VALIDATION_DEBOUNCE_MS = 600;

/**
 * Databases that can't be checked at all - no SQL validator configured, and a dialect the backend
 * can't parse in - so there's no point asking again. Rare: an engine without a validator is parsed
 * instead. Module-level rather than per-tab because it's a property of the database and the Superset
 * instance's configuration, which no amount of editing in a tab will change.
 */
const withoutValidator = new Set<number>();

/**
 * Keeps the active tab's SQL-error workflow in step with Superset's SQL validator - the same
 * validator that produces the error markers in the editor's gutter, on the same debounce,
 * so the panel is talking about exactly the errors the user can see. On an engine with no validator
 * the backend parses the SQL instead, so a syntax error still shows before the query is run.
 *
 * `paused` while an explanation is in flight: re-validating mid-call would either report the same
 * errors again or (once a fix lands) clear the very errors being explained. The effect re-runs when
 * it unpauses, which is what reconciles anything that changed in the meantime.
 */
export function useValidatorErrors(tab: sqlLab.Tab | undefined, sql: string, paused: boolean): void {
  const tabId = tab?.id;
  const databaseId = tab?.databaseId;
  const catalog = tab?.catalog ?? null;

  useEffect(() => {
    if (!tabId || paused) return undefined;
    if (sql.trim() === '') {
      recordValidatorErrors(tabId, sql, []);
      return undefined;
    }
    if (databaseId === undefined || withoutValidator.has(databaseId)) return undefined;

    const controller = new AbortController();
    const handle = window.setTimeout(() => {
      // No schema: queries here qualify their tables, so the tab's selected one isn't passed on.
      validateSql(sql, { databaseId, catalog, tabId }, controller.signal)
        .then(errors => recordValidatorErrors(tabId, sql, errors))
        .catch(error => {
          if (error instanceof NoValidatorError) {
            withoutValidator.add(databaseId);
            return;
          }
          // Anything else - the session expiring, the engine being unreachable, the validator
          // itself falling over - is Superset's own problem to report in the editor. Saying so
          // again here would be noise in a panel that has nothing to offer about it.
        });
    }, VALIDATION_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(handle);
      controller.abort();
    };
  }, [tabId, databaseId, catalog, sql, paused]);
}

/**
 * Picks up queries the database rejected, for the tab they were run in - the other half of what the
 * editor shows errors for, and the only half available at all on an engine with no validator
 * configured.
 *
 * Subscribed once for the whole panel and written straight into the failing tab's own state
 * (`context.tab`), not the active one: a run started in one tab can perfectly well fail while the
 * user is looking at another.
 */
export function useQueryFailureErrors(): void {
  useEffect(() => {
    const disposable = sqlLab.onDidQueryFail(context => {
      const sql = context.executedSql || '';
      if (!sql.trim()) return;
      recordQueryFailure(context.tab.id, sql, queryErrorsOf(context));
    });
    return () => disposable.dispose();
  }, []);
}

/**
 * The errors of a failed run, as this panel's workflow needs them. Superset's structured
 * `SupersetError`s are preferred over the bare message - they're the ones with an engine-specific
 * explanation attached - and their `extra.line_number`, where the engine reported one, lines them
 * up with the gutter the same way a validator annotation is.
 */
function queryErrorsOf(context: sqlLab.QueryErrorResultContext): SqlError[] {
  const structured = (context.errors ?? [])
    .filter(error => (error.message ?? '').trim() !== '')
    .map(error => ({
      message: error.message.trim(),
      line: typeof error.extra?.line_number === 'number' ? error.extra.line_number : undefined,
      source: 'query' as const,
    }));
  if (structured.length > 0) return structured;
  const message = (context.errorMessage || '').trim();
  return message === '' ? [] : [{ message, source: 'query' as const }];
}
