import React from 'react';
// The theme read directly rather than through `primitives`' `useTokens`, which imports this module.
import { theme as antdTheme, Tooltip } from 'antd';

/** A term the model marked as one a SQL user may not know, with what it means: `[[term|meaning]]`
 * (see the backend's `prose.py`). */
const TERM = /\[\[([^[\]|]+)\|([^[\]]*)\]\]/g;
/** The start of a term still being written - the text so far ends inside its markup. */
const PARTIAL = /\[\[([^[\]|]*)(\|[^[\]]*)?$/;

type Part = { text: string; meaning?: string; partial?: boolean };

function parse(text: string): Part[] {
  const parts: Part[] = [];
  let last = 0;
  const terms = new RegExp(TERM.source, 'g');
  for (let match = terms.exec(text); match; match = terms.exec(text)) {
    if (match.index > last) parts.push({ text: text.slice(last, match.index) });
    parts.push({ text: match[1], meaning: match[2].trim() });
    last = match.index + match[0].length;
  }
  const rest = text.slice(last);
  const partial = PARTIAL.exec(rest);
  if (partial) {
    if (partial.index > 0) parts.push({ text: rest.slice(0, partial.index) });
    if (partial[1]) parts.push({ text: partial[1], partial: true });
  } else if (rest) {
    parts.push({ text: rest });
  }
  return parts;
}

/** `text` with its term markup reduced to the terms - for anything that goes back into the editor,
 * the question box or the clipboard. */
export function plainText(text: string): string {
  return text.replace(TERM, (_match, term: string) => term);
}

/**
 * Prose the model wrote, with the terms it marked drawn a little stronger - underlined, on a faint
 * highlight - and their meaning in a tooltip on hover (or keyboard focus). A term still being
 * written while the text streams in shows as the term so far, never as half-written markup.
 * Text with no markup is drawn exactly as it is.
 */
export function Prose({ text }: { text: string }) {
  const { token } = antdTheme.useToken();
  if (!text.includes('[[')) return <>{text}</>;
  // Enough to be found when scanning the text - a visible tint and a 2px dotted underline a shade
  // darker than the info colour - but in the text's own colour and weight close to it, so it reads
  // as a hint rather than a link or a warning. Not `colorInfoBg`/`colorInfoBgHover`: on Superset's
  // pale info blue both come out all but white.
  const style: React.CSSProperties = {
    fontWeight: 500,
    color: token.colorText,
    background: token.colorInfoBorder,
    textDecorationLine: 'underline',
    textDecorationStyle: 'dotted',
    textDecorationColor: token.colorInfoActive,
    textDecorationThickness: 2,
    textUnderlineOffset: 3,
    borderRadius: 3,
    padding: '0 3px',
  };
  return (
    <>
      {parse(text).map((part, index) => {
        // eslint-disable-next-line react/no-array-index-key
        if (part.partial) return <span key={index} style={style}>{part.text}</span>;
        if (part.meaning === undefined) return <React.Fragment key={index}>{part.text}</React.Fragment>;
        return (
          // eslint-disable-next-line react/no-array-index-key
          <Tooltip key={index} title={part.meaning || undefined}>
            <span tabIndex={0} style={{ ...style, cursor: part.meaning ? 'help' : undefined }}>
              {part.text}
            </span>
          </Tooltip>
        );
      })}
    </>
  );
}
