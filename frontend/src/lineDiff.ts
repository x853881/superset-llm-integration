/** One line of a line diff: kept, removed from the original, or added in the rewrite. */
export interface DiffLine {
  kind: 'same' | 'removed' | 'added';
  text: string;
  /** Where the line is in the original and in the rewrite (0-based) - absent on the side it isn't on. */
  oldIndex?: number;
  newIndex?: number;
}

/** Above this many lines on either side the diff is skipped - a quadratic table that size is more
 * than a sidebar card is worth. */
const MAX_LINES = 600;

/**
 * A line-by-line diff of `before` -> `after` (longest common subsequence), comparing lines with
 * their surrounding whitespace ignored so that re-indenting isn't shown as a change. Null when
 * either side is too long to be worth diffing here.
 */
export function lineDiff(before: string, after: string): DiffLine[] | null {
  const a = before.replace(/\s+$/, '').split('\n');
  const b = after.replace(/\s+$/, '').split('\n');
  if (a.length > MAX_LINES || b.length > MAX_LINES) return null;
  const key = (line: string) => line.trim();
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = key(a[i]) === key(b[j]) ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (key(a[i]) === key(b[j])) {
      out.push({ kind: 'same', text: b[j], oldIndex: i, newIndex: j });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      out.push({ kind: 'removed', text: a[i], oldIndex: i });
      i += 1;
    } else {
      out.push({ kind: 'added', text: b[j], newIndex: j });
      j += 1;
    }
  }
  for (; i < a.length; i += 1) out.push({ kind: 'removed', text: a[i], oldIndex: i });
  for (; j < b.length; j += 1) out.push({ kind: 'added', text: b[j], newIndex: j });
  return out;
}

/** `sql` as `lineDiff` splits it - trailing whitespace dropped - so its line indices match. */
export function diffSource(sql: string): string {
  return sql.replace(/\s+$/, '');
}

/** One row of a side-by-side diff: a line on either side, or both (unchanged, or one replacing the
 * other). */
export interface DiffRow {
  kind: 'same' | 'changed';
  oldIndex: number | null;
  newIndex: number | null;
}

/**
 * `diff` laid out side by side: unchanged lines level with each other, and each run of changes with
 * its removed lines beside the lines that replaced them, row for row.
 */
export function sideBySide(diff: DiffLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let k = 0;
  while (k < diff.length) {
    const line = diff[k];
    if (line.kind === 'same') {
      rows.push({ kind: 'same', oldIndex: line.oldIndex ?? null, newIndex: line.newIndex ?? null });
      k += 1;
      continue;
    }
    const removed: number[] = [];
    const added: number[] = [];
    for (; k < diff.length && diff[k].kind !== 'same'; k += 1) {
      if (diff[k].kind === 'removed') removed.push(diff[k].oldIndex as number);
      else added.push(diff[k].newIndex as number);
    }
    for (let r = 0; r < Math.max(removed.length, added.length); r += 1) {
      rows.push({ kind: 'changed', oldIndex: removed[r] ?? null, newIndex: added[r] ?? null });
    }
  }
  return rows;
}
