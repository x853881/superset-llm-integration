import React from 'react';
import { Button, Flex, Tag, Typography } from 'antd';
import CloseOutlined from '@ant-design/icons/CloseOutlined';
import { OPTIONS_SHORTCUT, setOptionsOpen, useOptionsOpen } from '../devOptions';
import { LAYOUTS } from '../layouts';
import { LAYOUT_SHORTCUT, setPanelLayout, usePanelLayout } from '../panelLayout';
import { setBrief, useBrief } from '../verbosity';
import { SectionLabel, useTokens } from './primitives';

interface Choice {
  value: string;
  label: string;
  hint: string;
}

/** One thing the options page can switch: its choices, and how to read and set the current one -
 * each kept by its own module, so the page only shows and sets them. */
interface PanelOption {
  id: string;
  title: string;
  /** How it applies - at once, or to what is written next. */
  note?: string;
  choices: Choice[];
  defaultValue: string;
  useValue: () => string;
  setValue: (value: string) => void;
}

/** Everything the options page offers, in order. A new variant to try is one more entry. */
const OPTIONS: PanelOption[] = [
  {
    id: 'layout',
    title: 'Layout',
    note: `Applies at once. ${LAYOUT_SHORTCUT} steps through these too.`,
    choices: LAYOUTS.map(layout => ({ value: layout.id, label: layout.name, hint: layout.description })),
    defaultValue: LAYOUTS[0].id,
    useValue: usePanelLayout,
    setValue: setPanelLayout,
  },
  {
    id: 'verbosity',
    title: 'Descriptions and explanations',
    note: "The model's text changes from what is written next; the panel's own text at once. Also the header's Brief checkbox.",
    choices: [
      {
        value: 'full',
        label: 'Full',
        hint: "As long as the model judges useful, breakdowns with detailed explanations, and the panel's full help text.",
      },
      {
        value: 'brief',
        label: 'Brief',
        hint: "A sentence or a phrase each, no detailed explanations in breakdowns, and the panel's help text cut to a line.",
      },
    ],
    defaultValue: 'full',
    useValue: () => (useBrief() ? 'brief' : 'full'),
    setValue: value => setBrief(value === 'brief'),
  },
];

/**
 * The options page, laid over the whole side panel while open (see `devOptions`): each option's
 * choices as cards, the current one marked, applied as soon as one is picked and remembered in the
 * browser by the option's own module. For trying the panel's variants against each other while it
 * is being developed.
 */
export function OptionsOverlay() {
  const token = useTokens();
  const open = useOptionsOpen();
  // Every option's hook is called whether the page is open or not, so the order never changes.
  const values = OPTIONS.map(option => option.useValue());
  if (!open) return null;
  const allDefault = OPTIONS.every((option, index) => values[index] === option.defaultValue);
  return (
    <Flex
      vertical
      role="dialog"
      aria-label="Options"
      style={{
        position: 'absolute',
        inset: 0,
        zIndex: 10,
        background: token.colorBgContainer,
        boxShadow: token.boxShadowSecondary,
      }}
    >
      <Flex
        align="center"
        gap={token.marginXS}
        style={{
          flexShrink: 0,
          padding: `${token.paddingXS}px ${token.paddingSM}px`,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
        }}
      >
        <Typography.Text strong style={{ fontSize: token.fontSizeLG }}>
          Options
        </Typography.Text>
        <Tag color="purple" style={{ marginInlineEnd: 0 }}>
          development
        </Tag>
        <Typography.Text type="secondary" style={{ flex: 1, fontSize: token.fontSizeSM }}>
          {OPTIONS_SHORTCUT} or Esc to close
        </Typography.Text>
        <Button type="text" size="small" icon={<CloseOutlined />} aria-label="Close options" onClick={() => setOptionsOpen(false)} />
      </Flex>

      <Flex vertical gap={token.marginLG} style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: token.paddingSM }}>
        {OPTIONS.map((option, index) => (
          <Flex key={option.id} vertical gap={token.marginXS} role="radiogroup" aria-label={option.title}>
            <SectionLabel>{option.title}</SectionLabel>
            {option.choices.map(choice => {
              const selected = values[index] === choice.value;
              return (
                <button
                  key={choice.value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => option.setValue(choice.value)}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 2,
                    textAlign: 'left',
                    cursor: 'pointer',
                    fontFamily: token.fontFamily,
                    padding: `${token.paddingXS}px ${token.paddingSM}px`,
                    borderRadius: token.borderRadiusLG,
                    border: `1px solid ${selected ? token.colorPrimary : token.colorBorderSecondary}`,
                    boxShadow: selected ? `0 0 0 1px ${token.colorPrimary}` : undefined,
                    background: selected ? token.colorPrimaryBg : token.colorBgContainer,
                  }}
                >
                  <Typography.Text strong style={{ color: selected ? token.colorPrimaryText : undefined }}>
                    {choice.label}
                    {choice.value === option.defaultValue && (
                      <Typography.Text type="secondary" style={{ fontWeight: 'normal', fontSize: token.fontSizeSM }}>
                        {' '}
                        (default)
                      </Typography.Text>
                    )}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                    {choice.hint}
                  </Typography.Text>
                </button>
              );
            })}
            {option.note && (
              <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
                {option.note}
              </Typography.Text>
            )}
          </Flex>
        ))}
      </Flex>

      <Flex
        align="center"
        justify="space-between"
        gap={token.marginXS}
        style={{ flexShrink: 0, padding: `${token.paddingXS}px ${token.paddingSM}px`, borderTop: `1px solid ${token.colorBorderSecondary}` }}
      >
        <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
          Remembered in this browser.
        </Typography.Text>
        <Button
          size="small"
          disabled={allDefault}
          onClick={() => OPTIONS.forEach(option => option.setValue(option.defaultValue))}
        >
          Reset to defaults
        </Button>
      </Flex>
    </Flex>
  );
}
