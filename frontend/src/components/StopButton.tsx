import React from 'react';
import { Button, Tooltip } from 'antd';
import { SquareIcon, useTokens } from './primitives';

export interface StopButtonProps {
  onStop: () => void;
  /** What stopping would abandon, for the tooltip - "this request", "the explanation", ... */
  what: string;
  /** Small, to sit in a line of text (Alpha's progress header) rather than as a call to action. */
  small?: boolean;
}

/**
 * Stop, presented exactly as SQL Lab presents stopping a running query: a solid danger-coloured
 * button with a filled square and the word "Stop", sized as a call to action.
 *
 * Deliberately the same button rather than a small secondary one. Stopping a generation and
 * stopping a query are the same act from the user's side - calling off something that is running
 * and taking its time - and SQL Lab has already taught them what that button looks like and where
 * it sits. Matched to `RunQueryActionButton` in the host
 * (`superset-frontend/src/SqlLab/components/RunQueryActionButton`): `buttonStyle="danger"` is
 * antd's solid danger, `cta` is its minimum width and height in size units, and the icon is the
 * host's own `Icons.Square` (see `SquareIcon`).
 */
export function StopButton({ onStop, what, small = false }: StopButtonProps) {
  const token = useTokens();
  return (
    <Tooltip title={`Stop ${what}`}>
      <Button
        type="primary"
        danger
        size={small ? 'small' : undefined}
        icon={<SquareIcon />}
        onClick={onStop}
        style={small ? undefined : {
          alignSelf: 'flex-start',
          // The host's `cta` sizing, in the same units it uses.
          minWidth: token.sizeUnit * 36,
          minHeight: token.sizeUnit * 8,
        }}
      >
        Stop
      </Button>
    </Tooltip>
  );
}
