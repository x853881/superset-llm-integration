/**
 * The way of picking tables for the SQL prompt this browser asks for - see the backend's
 * `table_selection`. Sent with every call (see `api.ts`); the backend uses it only if the site allows
 * that option and the user has a role that may choose, and its own default otherwise.
 */

import { useEffect, useState } from 'react';

const STORAGE_KEY = 'nlsql.tableSelection';
const listeners = new Set<(name: string | null) => void>();

export function getTableSelection(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setTableSelection(name: string | null): void {
  try {
    if (name) window.localStorage.setItem(STORAGE_KEY, name);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage refused: requests get the site's default.
  }
  listeners.forEach(listener => listener(getTableSelection()));
}

export function useTableSelection(): string | null {
  const [name, setName] = useState(getTableSelection);
  useEffect(() => {
    listeners.add(setName);
    setName(getTableSelection());
    return () => {
      listeners.delete(setName);
    };
  }, []);
  return name;
}
