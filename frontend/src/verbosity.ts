/**
 * Whether the panel asks the model for brief prose - set by the chosen theme (see `panelTheme.ts`).
 *
 * Sent with every call (see `api.ts`), and the backend tells the model which form to write (see its
 * `prose.py`) rather than asking for both and showing one: what a call costs in time is mostly the
 * text it writes, so writing both would make every call slower, brief ones included. So switching
 * theme changes what is written next; what is already on screen stays as it was written.
 *
 * It shortens the panel's own fixed text too - its notes, hints, empty states and tooltips - to a
 * line where it can (see `useWording`). That changes at once, since nothing has to be written again.
 */

import { getPanelTheme, usePanelTheme } from './panelTheme';

export type Verbosity = 'brief' | 'full';

export function getVerbosity(): Verbosity {
  return getPanelTheme().verbosity;
}

/** Whether brief is on now, kept current. */
export function useBrief(): boolean {
  return usePanelTheme().verbosity === 'brief';
}

/** Picks the panel's fixed text to match: `say(full, brief)`, where the brief form fits on a line
 * where it can - or leaves something out altogether, given `null`. */
export function useWording(): <Full, Brief>(full: Full, brief: Brief) => Full | Brief {
  const brief = useBrief();
  return (full, short) => (brief ? short : full);
}
