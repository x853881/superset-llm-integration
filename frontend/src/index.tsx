import React from 'react';
import { views } from '@apache-superset/core';
import NLSqlHelperPanel from './NLSqlHelperPanel';
import { BreakdownPane } from './components/BreakdownPane';
import { EstimatorPane } from './components/EstimatorPane';
import { watchCompareView } from './compareView';

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
  { id: 'waxwing.nltosqlandback.breakdown', name: 'Break it down' },
  'sqllab.panels',
  () => <BreakdownPane />,
);
views.registerView(
  { id: 'waxwing.nltosqlandback.query-estimator', name: 'Query Estimator' },
  'sqllab.panels',
  () => <EstimatorPane />,
);

// A third, only while the active tab's SQL was accelerated from something else: the two compared.
// Registered and removed as that changes (see `compareView`), so it isn't registered here.
watchCompareView();
