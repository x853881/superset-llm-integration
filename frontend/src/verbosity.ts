/**
 * Whether the panel asks the model for brief prose - the "Brief" checkbox in the panel header.
 *
 * Sent with every call (see `api.ts`), and the backend tells the model which form to write (see its
 * `prose.py`) rather than asking for both and showing one: what a call costs in time is mostly the
 * text it writes, so writing both would make every call slower, brief ones included. So switching
 * it changes what is written next; what is already on screen stays as it was written.
 *
 * It shortens the panel's own fixed text too - its notes, hints, empty states and tooltips - to a
 * line where it can (see `useWording`). That changes at once, since nothing has to be written again.
 *
 * A display preference, kept in the browser's storage like the layout (`panelLayout.ts`) - off
 * when that storage is unavailable.
 */

import { useEffect, useState } from 'react';

export type Verbosity = 'brief' | 'full';

const STORAGE_KEY = 'waxwing.nltosqlandback.brief';

function stored(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

let brief = stored();
const listeners = new Set<(brief: boolean) => void>();

export function getVerbosity(): Verbosity {
  return brief ? 'brief' : 'full';
}

export function setBrief(value: boolean): void {
  brief = value;
  try {
    window.localStorage.setItem(STORAGE_KEY, String(value));
  } catch {
    // It still applies; it just won't be remembered.
  }
  listeners.forEach(listener => listener(value));
}

/** Whether brief is on now, kept current. */
export function useBrief(): boolean {
  const [value, setValue] = useState(brief);
  useEffect(() => {
    listeners.add(setValue);
    setValue(brief);
    return () => {
      listeners.delete(setValue);
    };
  }, []);
  return value;
}

/** Picks the panel's fixed text to match: `say(full, brief)`, where the brief form fits on a line
 * where it can - or leaves something out altogether, given `null`. */
export function useWording(): <Full, Brief>(full: Full, brief: Brief) => Full | Brief {
  const brief = useBrief();
  return (full, short) => (brief ? short : full);
}
