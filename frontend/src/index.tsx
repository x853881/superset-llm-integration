import React from 'react';
import { views } from '@apache-superset/core';
import NLSqlHelperPanel from './NLSqlHelperPanel';
import { BreakdownPane } from './components/BreakdownPane';
import { EstimatorPane } from './components/EstimatorPane';
import { watchCompareView } from './compareView';
import { BREAKDOWN_VIEW_ID } from './breakdownAction';

// Set at build time (see webpack.config.js).
declare const __NLSQL_IGNORE_ROLES__: boolean;
declare const __NLSQL_BUILD__: string;

/**
 * Whether the current user may see the panel, and why: they need a role with some permission on
 * `nltosqlandback`, which the host's security manager gives the EXTENSIONS role (and Admin).
 */
function access(): { shown: boolean; why: string } {
  if (__NLSQL_IGNORE_ROLES__) {
    return { shown: true, why: 'shown to everyone (built with NLSQL_IGNORE_ROLES=true)' };
  }
  let roles: Record<string, [string, string][]>;
  try {
    const raw = document.getElementById('app')?.getAttribute('data-bootstrap');
    roles = raw ? JSON.parse(raw).user?.roles ?? {} : {};
  } catch {
    return { shown: false, why: "hidden: couldn't read your roles from the page" };
  }
  const names = Object.keys(roles);
  const allowing = names.filter(name => roles[name].some(([, viewMenu]) => viewMenu === 'nltosqlandback'));
  if (allowing.length) {
    return { shown: true, why: `shown: your role ${allowing.join(', ')} has a permission on nltosqlandback` };
  }
  return {
    shown: false,
    why:
      `hidden: none of your roles (${names.join(', ') || 'none found'}) has a permission on nltosqlandback. ` +
      'Give one of them a permission on nltosqlandback, or build with NLSQL_IGNORE_ROLES=true',
  };
}

function registerViews() {
  // Registered at module load time - Superset requests this ./index module via
  // Module Federation and runs it as a side effect.
  //
  // This panel sits in SQL Lab's right sidebar alongside Superset's own SQL editor (Ace or
  // Monaco), rather than replacing that editor outright the way an earlier version of this
  // extension did (via `editors.registerEditor`) - all of the panel's NL/refine/undo-redo
  // functionality now lives here instead, so the editor itself stays exactly what it always was.
  views.registerView(
    { id: 'waxwing.nltosqlandback.nl-sql-helper', name: 'NL to SQL and back' },
    'sqllab.rightSidebar',
    () => <NLSqlHelperPanel />,
  );

  // Two tabs under the editor, beside Results and Query history. Superset renders every one of them
  // all the time (hidden when not selected), so each does nothing expensive until it is on screen.
  views.registerView(
    { id: BREAKDOWN_VIEW_ID, name: 'Break it down' },
    'sqllab.panels',
    () => <BreakdownPane />,
  );
  views.registerView(
    { id: 'waxwing.nltosqlandback.query-estimator', name: 'Query Estimator' },
    'sqllab.panels',
    () => <EstimatorPane />,
  );
}

// The first says the bundle loaded at all, and which build it is; the second why the panel is or
// isn't shown. Both are also on window.nltosqlandback, for checking later from the console.
console.info(`[AI Assistant] loaded, build ${__NLSQL_BUILD__}`);
const { shown, why } = access();
if (shown) {
  console.info(`[AI Assistant] panel ${why}`);
  registerViews();
} else {
  console.warn(`[AI Assistant] panel ${why}`);
}
(window as unknown as Record<string, unknown>).nltosqlandback = { build: __NLSQL_BUILD__, shown, why };

// A third, only while the active tab's SQL was accelerated from something else: the two compared.
// Registered and removed as that changes (see `compareView`), so it isn't registered here.
watchCompareView();
