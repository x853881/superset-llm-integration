/**
 * A small SQL tokenizer for drawing a query read-only with its keywords, strings, numbers and
 * comments coloured - the breakdown tab's left-hand side. Not a parser: it only has to tell those
 * apart well enough to colour them, across every dialect, without a dependency.
 *
 * Tokens are returned per line, because the breakdown marks sections by line: a block comment or
 * string spanning lines is split into one token per line it touches.
 */

export type SqlTokenKind =
  | 'keyword'
  | 'function'
  | 'type'
  | 'string'
  | 'number'
  | 'comment'
  | 'identifier'
  | 'quoted'
  | 'operator'
  | 'punctuation'
  | 'space';

export interface SqlToken {
  kind: SqlTokenKind;
  text: string;
}

const KEYWORDS = new Set(
  `select from where and or not in is null as on join inner left right full outer cross natural using group by order
  having limit offset fetch first next rows row only union all intersect except distinct case when then else end with
  recursive exists between like ilike similar any some asc desc nulls last insert into values update set delete create
  table view materialized index drop alter add column primary key foreign references default unique check constraint
  over partition window range unbounded preceding following current filter within lateral true false interval
  qualify top returning merge matched escape collate grant revoke truncate replace temp temporary if cascade
  restrict begin commit rollback explain analyze tablesample unnest array struct map at time zone`
    .split(/\s+/)
    .filter(Boolean),
);

const TYPES = new Set(
  `int integer bigint smallint tinyint decimal numeric real float double precision boolean bool char varchar text
  string date timestamp timestamptz datetime json jsonb uuid bytea varbinary binary number varchar2 nvarchar clob blob`
    .split(/\s+/)
    .filter(Boolean),
);

/** `sql` as tokens, one array per line. */
export function highlightSql(sql: string): SqlToken[][] {
  const lines: SqlToken[][] = [[]];
  const push = (kind: SqlTokenKind, text: string) => {
    const parts = text.split('\n');
    parts.forEach((part, index) => {
      if (index > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ kind, text: part });
    });
  };

  let i = 0;
  const n = sql.length;
  while (i < n) {
    const c = sql[i];
    const rest = sql.slice(i);

    if (c === '-' && sql[i + 1] === '-') {
      const end = sql.indexOf('\n', i);
      const stop = end < 0 ? n : end;
      push('comment', sql.slice(i, stop));
      i = stop;
      continue;
    }
    if (c === '/' && sql[i + 1] === '*') {
      const end = sql.indexOf('*/', i + 2);
      const stop = end < 0 ? n : end + 2;
      push('comment', sql.slice(i, stop));
      i = stop;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < n) {
        if (sql[j] === c) {
          if (sql[j + 1] === c) {
            j += 2; // a doubled quote is an escaped one
            continue;
          }
          break;
        }
        if (sql[j] === '\\' && c === "'") j += 1;
        j += 1;
      }
      const stop = Math.min(n, j + 1);
      push(c === "'" ? 'string' : 'quoted', sql.slice(i, stop));
      i = stop;
      continue;
    }
    if (c === '[') {
      const end = sql.indexOf(']', i);
      if (end > i && !sql.slice(i, end).includes('\n')) {
        push('quoted', sql.slice(i, end + 1));
        i = end + 1;
        continue;
      }
    }
    const space = /^\s+/.exec(rest);
    if (space) {
      push('space', space[0]);
      i += space[0].length;
      continue;
    }
    const number = /^(\d+\.?\d*(e[+-]?\d+)?|\.\d+)/i.exec(rest);
    if (number && !/[A-Za-z_]/.test(sql[i - 1] ?? '')) {
      push('number', number[0]);
      i += number[0].length;
      continue;
    }
    const word = /^[A-Za-z_][A-Za-z0-9_$]*/.exec(rest);
    if (word) {
      const lower = word[0].toLowerCase();
      const after = sql.slice(i + word[0].length).match(/^\s*\(/);
      let kind: SqlTokenKind = 'identifier';
      if (TYPES.has(lower) && (after || /::\s*$/.test(sql.slice(0, i)))) kind = 'type';
      else if (KEYWORDS.has(lower)) kind = 'keyword';
      else if (after) kind = 'function';
      push(kind, word[0]);
      i += word[0].length;
      continue;
    }
    const operator = /^(<>|!=|<=|>=|\|\||::|->>|->|[=<>+\-*/%^~!|&])/.exec(rest);
    if (operator) {
      push('operator', operator[0]);
      i += operator[0].length;
      continue;
    }
    push('punctuation', c);
    i += 1;
  }
  return lines;
}
