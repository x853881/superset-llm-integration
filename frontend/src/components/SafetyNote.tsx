import React from 'react';
import { Flex, Typography } from 'antd';
import { components } from '@apache-superset/core';
import ExclamationCircleOutlined from '@ant-design/icons/ExclamationCircleOutlined';
import { useDismissible } from '../notices';
import { useWording } from '../verbosity';
import { useTokens } from './primitives';

/**
 * How the panel behaves: SQL is written into the editor and left there. Running it stays an
 * explicit act by the user, with the same limits and permissions as anything they typed by hand.
 *
 * Closeable, and stays closed (see `useDismissible`) - it's an explanation of a mechanism, which is
 * worth reading once and then takes up room in a 400px-wide panel forever. What isn't closeable is
 * `ResponsibilityNote` below, which is a different kind of statement.
 */
export function SafetyNote({ refinement }: { refinement: boolean }) {
  const token = useTokens();
  // Two separate notices, not one with two wordings: closing the refinement one shouldn't silently
  // close an explanation the user has never seen.
  const [visible, dismiss] = useDismissible(refinement ? 'how-refinement-works' : 'how-generation-works');
  const say = useWording();
  if (!visible) return null;
  const run = <Typography.Text strong>Run</Typography.Text>;
  return (
    <components.Alert
      type="info"
      showIcon
      closable
      onClose={dismiss}
      style={{ fontSize: token.fontSizeSM, padding: `${token.paddingXS}px ${token.paddingSM}px` }}
    >
      {refinement
        ? say(
            <>
              A refinement or an applied faster version rewrites the SQL in the editor and waits for {run} again.
              Accelerate and the plan explanation only ask the database to plan the query — it is never run.
            </>,
            <>Changes go into the editor; you press {run}.</>,
          )
        : say(
            <>
              Generated SQL is placed in the editor for you to read. Nothing is ever run for you — you press {run}.
            </>,
            <>SQL goes into the editor; you press {run}.</>,
          )}
    </components.Alert>
  );
}

/**
 * That the model can be wrong, and that checking it is the user's job - kept in view at all times
 * and deliberately not closeable.
 *
 * Every query this panel writes is a guess at what was meant, made by a language model from a
 * description of the schema: it can pick the wrong column, quietly change what a filter includes,
 * or produce something that runs perfectly and answers a different question. Whoever presses Run
 * owns what it does, so the statement of that stays on screen rather than being shown once and
 * dismissed - which is also why it's a plain line of text rather than an alert: something present
 * always has to be quiet enough to live with.
 */
export function ResponsibilityNote() {
  const token = useTokens();
  const say = useWording();
  return (
    <Flex gap={token.marginXXS} align="flex-start">
      <ExclamationCircleOutlined
        style={{ color: token.colorTextTertiary, fontSize: token.fontSizeSM, marginTop: 3 }}
      />
      <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
        {say(
          'AI can make mistakes. Check what a query does before you run it — the results, and anything you decide from them, are your responsibility.',
          'AI can make mistakes — check a query before you run it.',
        )}
      </Typography.Text>
    </Flex>
  );
}
