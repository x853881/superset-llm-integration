/**
 * Accelerate's changes as notes on the "Compare with original" diff (see `ComparePane`): each change
 * found again, by the SQL it quoted, in the two versions being compared.
 *
 * A change keeps what it replaced (`before`) and what replaced it (`after`) as text, not positions
 * (see `RewriteChange`), because the two versions on screen are often not the two it was written
 * about: the comparison can be with the original two accelerations back, or with SQL edited by hand
 * since. So each fragment is looked for in the SQL actually shown, as tokens - spacing, line breaks,
 * comments and the case of keywords and names don't matter, string literals and quoted names must be
 * exact - the same way the backend's `change_anchors.py` checked it (keep the two alike).
 *
 * A change found on neither side has been undone or rewritten again since, so its reasoning no longer
 * describes what is on screen, and it is left out. A change that quoted nothing (or whose quotes the
 * backend couldn't find) is still shown, just not beside any line.
 */

import type { SqlVersion } from './ancestry';
import type { RewriteChange } from './performanceResult';

interface Token {
  /** What tokens are compared by: the text, lower-cased unless it is a string or quoted name. */
  key: string;
  start: number;
  end: number;
}

// One token at a time; groups: 1 space, 2 comment, 3 string, 4 quoted, then the rest. Mirrors
// `change_anchors._TOKEN` in the backend.
const TOKEN =
  /(\s+)|(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^'\\]|\\[\s\S]|'')*(?:'|$))|("(?:[^"]|"")*(?:"|$)|`(?:[^`]|``)*(?:`|$)|\[[^\]\n]*\])|(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_$]*)|(<>|!=|<=|>=|\|\||::|->>|->|[=<>+\-*/%^~!|&])|([\s\S])/g;

function tokens(sql: string): Token[] {
  const out: Token[] = [];
  const pattern = new RegExp(TOKEN.source, 'g');
  for (let match = pattern.exec(sql); match; match = pattern.exec(sql)) {
    if (match[1] !== undefined || match[2] !== undefined) continue;
    const exact = match[3] !== undefined || match[4] !== undefined;
    out.push({ key: exact ? match[0] : match[0].toLowerCase(), start: match.index, end: match.index + match[0].length });
  }
  return out;
}

/** A stretch of the SQL, as character offsets [start, end). */
export interface CharRange {
  start: number;
  end: number;
}

/** Where `fragment` is in `sql`: its first occurrence as tokens, or null when it's empty or not there. */
export function locate(fragment: string, sql: string): CharRange | null {
  const wanted = tokens(fragment).map(token => token.key);
  if (wanted.length === 0) return null;
  const have = tokens(sql);
  for (let index = 0; index + wanted.length <= have.length; index += 1) {
    let k = 0;
    while (k < wanted.length && have[index + k].key === wanted[k]) k += 1;
    if (k === wanted.length) return { start: have[index].start, end: have[index + wanted.length - 1].end };
  }
  return null;
}

/** A change found in the comparison: where its `before` is in the older SQL, its `after` in the newer. */
export interface PlacedNote {
  /** 1, 2, 3… in the order they were made. */
  number: number;
  change: RewriteChange;
  before: CharRange | null;
  after: CharRange | null;
}

export interface ComparisonNotes {
  placed: PlacedNote[];
  /** Changes that quoted nothing to place them by. */
  unplaced: RewriteChange[];
}

/**
 * The notes for comparing `base` (one of `versions`) - drawn as `oldText` - with `newText`: the
 * changes of every acceleration since `base`, oldest first, each placed where its fragments are.
 */
export function notesFor(versions: SqlVersion[], base: SqlVersion, oldText: string, newText: string): ComparisonNotes {
  const since = versions.slice(versions.indexOf(base) + 1);
  const changes = since.reduce<RewriteChange[]>(
    (all, version) => (Array.isArray(version.changes) ? all.concat(version.changes) : all),
    [],
  );
  const placed: PlacedNote[] = [];
  const unplaced: RewriteChange[] = [];
  changes.forEach(change => {
    if (!change.before && !change.after) {
      if (change.summary || change.why) unplaced.push(change);
      return;
    }
    const before = change.before ? locate(change.before, oldText) : null;
    const after = change.after ? locate(change.after, newText) : null;
    if (before || after) placed.push({ number: placed.length + 1, change, before, after });
  });
  return { placed, unplaced };
}

/** Which line (0-based) of `text` character `offset` is on. */
export function lineOf(text: string, offset: number): number {
  let line = 0;
  for (let index = 0; index < offset && index < text.length; index += 1) if (text[index] === '\n') line += 1;
  return line;
}

/**
 * `ranges` cut into per-line column spans - what to underline on each line of `text` - as a map from
 * line (0-based) to [start, end) columns.
 */
export function spansByLine(text: string, ranges: CharRange[]): Map<number, [number, number][]> {
  const starts = [0];
  for (let index = 0; index < text.length; index += 1) if (text[index] === '\n') starts.push(index + 1);
  const out = new Map<number, [number, number][]>();
  ranges.forEach(range => {
    const first = lineOf(text, range.start);
    const last = lineOf(text, Math.max(range.start, range.end - 1));
    for (let line = first; line <= last; line += 1) {
      const lineStart = starts[line];
      const lineEnd = line + 1 < starts.length ? starts[line + 1] - 1 : text.length;
      const span: [number, number] = [Math.max(range.start, lineStart) - lineStart, Math.min(range.end, lineEnd) - lineStart];
      if (span[1] > span[0]) out.set(line, [...(out.get(line) ?? []), span]);
    }
  });
  return out;
}
