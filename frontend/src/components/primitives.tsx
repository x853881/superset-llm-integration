import React from 'react';
import { Flex, theme as antdTheme, Typography } from 'antd';
import CloseCircleFilled from '@ant-design/icons/CloseCircleFilled';
import WarningFilled from '@ant-design/icons/WarningFilled';
import { Prose } from './Prose';

/**
 * Superset's own live Ant Design design tokens - colours, spacing, radii, type scale - read out
 * of the `ConfigProvider` the host app wraps SQL Lab in. Everything this panel draws itself
 * (rather than delegating to an antd component) is sized and coloured from these, so the panel
 * follows whatever theme the Superset instance is running, light or dark, without hardcoding a
 * palette of its own.
 *
 * `antd` itself is not bundled with this extension - it's a Module Federation singleton shared by
 * the host (see `webpack.config.js`), so these are literally the same tokens Superset's own
 * components are drawing with, not a second copy configured to look similar.
 */
export type Tokens = ReturnType<typeof antdTheme.useToken>['token'];

export function useTokens(): Tokens {
  return antdTheme.useToken().token;
}

/** The small upper-case label that heads each block of result detail ("TABLES IT USED", ...). */
export function SectionLabel({ children }: { children: React.ReactNode }) {
  const token = useTokens();
  return (
    <div
      style={{
        fontSize: token.fontSizeSM,
        fontWeight: token.fontWeightStrong,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        color: token.colorTextTertiary,
      }}
    >
      {children}
    </div>
  );
}

export function BulletList({ items }: { items: string[] }) {
  const token = useTokens();
  return (
    <ul
      style={{
        margin: 0,
        paddingLeft: token.paddingMD,
        color: token.colorText,
        fontSize: token.fontSize,
        lineHeight: token.lineHeight,
      }}
    >
      {items.map(item => (
        <li key={item} style={{ marginBottom: token.marginXXS }}>
          <Prose text={item} />
        </li>
      ))}
    </ul>
  );
}

/** How much something needs the reader: a warning (worth knowing before going on) or an error
 * (something is wrong). */
export type Tone = 'warning' | 'error';

/** A warning's text stays the ordinary text colour: the theme's warning colour is a yellow that is
 * hard to read as text, so the mark, the bar and the tint say "warning" instead. */
function toneColours(token: Tokens, tone: Tone) {
  return tone === 'error'
    ? { solid: token.colorError, text: token.colorErrorText, tint: token.colorErrorBg, border: token.colorErrorBorder }
    : { solid: token.colorWarning, text: token.colorText, tint: token.colorWarningBg, border: token.colorWarningBorder };
}

/** The mark for a warning or an error - filled, not outlined, so it reads at a glance among the
 * panel's grey outline icons. */
export function ToneIcon({ tone, style }: { tone: Tone; style?: React.CSSProperties }) {
  const token = useTokens();
  const Icon = tone === 'error' ? CloseCircleFilled : WarningFilled;
  return <Icon style={{ color: toneColours(token, tone).solid, ...style }} />;
}

/**
 * A box that needs the reader's attention: a pale tint of the tone's colour, its border, and a bar
 * of the full colour down the left edge - plain to see in a column of grey boxes without shouting.
 * `tinted={false}` keeps the border and the bar but not the tint, for a card that is mostly other
 * content (the Fix card's explanation and corrected SQL).
 */
export function Callout({
  tone,
  tinted = true,
  gap,
  style,
  children,
}: {
  tone: Tone;
  tinted?: boolean;
  gap?: number;
  style?: React.CSSProperties;
  children: React.ReactNode;
}) {
  const token = useTokens();
  const colours = toneColours(token, tone);
  return (
    <Flex
      vertical
      gap={gap ?? token.marginXS}
      style={{
        padding: `${token.paddingXS}px ${token.paddingSM}px`,
        borderRadius: token.borderRadius,
        border: `1px solid ${colours.border}`,
        borderLeft: `3px solid ${colours.solid}`,
        background: tinted ? colours.tint : token.colorBgContainer,
        ...style,
      }}
    >
      {children}
    </Flex>
  );
}

/** A warning or an error on a line of its own inside something else: the mark, then the text -
 * red for an error, ordinary for a warning (see `toneColours`). */
export function Flag({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  const token = useTokens();
  return (
    <Flex gap={token.marginXXS} align="flex-start">
      <ToneIcon tone={tone} style={{ fontSize: token.fontSizeSM, marginTop: 4, flexShrink: 0 }} />
      <Typography.Text style={{ fontSize: token.fontSizeSM, color: toneColours(token, tone).text, wordBreak: 'break-word' }}>
        {children}
      </Typography.Text>
    </Flex>
  );
}

/**
 * The "ask" mark used on the panel header and the generate button. Drawn inline rather than
 * pulled from an icon set so it inherits `currentColor` and stays a single small shape at any
 * font size.
 */
export function SparkleIcon({ style }: { style?: React.CSSProperties }) {
  return (
    <svg viewBox="0 0 16 16" width="1em" height="1em" fill="currentColor" aria-hidden="true" style={style}>
      <path d="M9.1 1.2 10.2 4l2.8 1.1-2.8 1.1-1.1 2.8L8 6.2 5.2 5.1 8 4z" />
      <path d="M4.2 8.6 4.9 10.4l1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" />
    </svg>
  );
}

/**
 * The filled, slightly-rounded square Superset draws on SQL Lab's own Stop button
 * (`superset-frontend/src/assets/images/icons/square.svg`, its `Icons.Square`) - redrawn here
 * rather than imported because an extension can't reach into the host's icon set, and the point of
 * this panel's Stop button is to be the same button (see `StopButton`).
 */
export function SquareIcon({ style }: { style?: React.CSSProperties }) {
  return (
    <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden="true" style={style}>
      <rect width="24" height="24" rx="3" />
    </svg>
  );
}

/** "⌘↵" on a Mac, "Ctrl+↵" everywhere else - for the "or press ..." hint beside a submit button. */
export function submitShortcutLabel(): string {
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.platform);
  return isMac ? '⌘↵' : 'Ctrl+↵';
}

/** True if `event` is the "submit this box" chord that `submitShortcutLabel` advertises. */
export function isSubmitShortcut(event: React.KeyboardEvent): boolean {
  return event.key === 'Enter' && (event.metaKey || event.ctrlKey);
}
