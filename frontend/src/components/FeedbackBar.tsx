import React, { useState } from 'react';
import { Button, Flex, Input, Tooltip, Typography } from 'antd';
import DislikeFilled from '@ant-design/icons/DislikeFilled';
import DislikeOutlined from '@ant-design/icons/DislikeOutlined';
import LikeFilled from '@ant-design/icons/LikeFilled';
import LikeOutlined from '@ant-design/icons/LikeOutlined';
import { FeedbackChange, FeedbackTarget, giveFeedback, Reason, reasonsFor, useFeedback } from '../feedback';
import type { TraceRef } from '../progress';
import { useBrief, useWording } from '../verbosity';
import { useTokens } from './primitives';

export interface FeedbackBarProps {
  /** The request the result came from; nothing is drawn without one (a result from before the
   * backend sent traces, say). */
  trace?: TraceRef;
  target: FeedbackTarget;
}

/**
 * "Helpful? 👍 👎" under a result, small and in the quietest text colour, at the end of the line -
 * there to be used, not to be noticed first. Either thumb is one click and is sent straight away. A
 * thumbs down then offers the likeliest reasons for this kind of result (see `reasonsFor`), each
 * one click more; "Other…" is the only way to a text box. See `feedback.ts`.
 */
export function FeedbackBar({ trace, target }: FeedbackBarProps) {
  const token = useTokens();
  const brief = useBrief();
  const say = useWording();
  const given = useFeedback(trace);
  // The reasons, offered after a thumbs down (and again on "Change").
  const [choosing, setChoosing] = useState(false);
  // The "Other…" box's text while it is open.
  const [other, setOther] = useState<string | null>(null);
  if (!trace) return null;

  const give = (change: FeedbackChange) => giveFeedback(trace, target, change);
  const rate = (rating: 'up' | 'down') => {
    give({ rating });
    setChoosing(rating === 'down');
    setOther(null);
  };
  const pick = (reason: Reason) => {
    give({ rating: 'down', reason: reason.id, label: reason.label });
    setChoosing(false);
  };
  const sendOther = () => {
    const comment = (other ?? '').trim();
    if (!comment) return;
    give({ rating: 'down', reason: 'other', comment });
    setOther(null);
    setChoosing(false);
  };

  const quiet: React.CSSProperties = { fontSize: token.fontSizeSM, color: token.colorTextTertiary };
  const said = given?.label ?? given?.comment;
  const status =
    given === undefined ? (
      say('Was this helpful?', 'Helpful?')
    ) : given.status === 'failed' ? (
      <>
        <span style={{ color: token.colorText }}>Couldn't send that.</span>{' '}
        <Typography.Link style={{ fontSize: token.fontSizeSM }} onClick={() => give({ rating: given.rating, reason: given.reason, label: given.label, comment: given.comment })}>
          Try again
        </Typography.Link>
      </>
    ) : given.rating === 'down' && choosing ? (
      'What was wrong?'
    ) : said ? (
      <Typography.Text ellipsis style={{ ...quiet, maxWidth: 220 }}>
        Thanks - {said}
      </Typography.Text>
    ) : (
      'Thanks'
    );

  const thumb = (rating: 'up' | 'down') => {
    const chosen = given?.rating === rating;
    const Icon = rating === 'up' ? (chosen ? LikeFilled : LikeOutlined) : chosen ? DislikeFilled : DislikeOutlined;
    const label = rating === 'up' ? 'Helpful' : 'Not helpful';
    return (
      <Tooltip title={label} mouseEnterDelay={0.6}>
        <Button
          type="text"
          size="small"
          aria-label={label}
          aria-pressed={chosen}
          icon={<Icon style={{ fontSize: token.fontSizeSM, color: chosen ? token.colorPrimary : token.colorTextTertiary }} />}
          onClick={() => (chosen && rating === 'down' ? setChoosing(!choosing) : rate(rating))}
        />
      </Tooltip>
    );
  };

  return (
    <Flex vertical gap={token.marginXXS} style={{ alignSelf: 'stretch' }}>
      <Flex align="center" justify="flex-end" gap={2} style={quiet}>
        <span style={{ marginInlineEnd: token.marginXXS }}>{status}</span>
        {given?.rating === 'down' && said && !choosing && (
          <Typography.Link style={{ fontSize: token.fontSizeSM, marginInlineEnd: token.marginXXS }} onClick={() => setChoosing(true)}>
            Change
          </Typography.Link>
        )}
        {thumb('up')}
        {thumb('down')}
      </Flex>
      {given?.rating === 'down' && choosing && (
        <Flex wrap="wrap" justify="flex-end" gap={token.marginXXS}>
          {other === null ? (
            <>
              {reasonsFor(target, brief).map(reason => (
                <Button key={reason.id} size="small" style={{ fontSize: token.fontSizeSM }} onClick={() => pick(reason)}>
                  {reason.label}
                </Button>
              ))}
              <Button size="small" type="dashed" style={{ fontSize: token.fontSizeSM }} onClick={() => setOther('')}>
                Other…
              </Button>
            </>
          ) : (
            <Flex gap={token.marginXXS} style={{ width: '100%' }}>
              <Input
                size="small"
                autoFocus
                maxLength={2000}
                placeholder="What was wrong?"
                value={other}
                onChange={event => setOther(event.target.value)}
                onPressEnter={sendOther}
                onKeyDown={event => {
                  if (event.key === 'Escape') {
                    event.stopPropagation();
                    setOther(null);
                  }
                }}
              />
              <Button size="small" type="primary" disabled={!other.trim()} onClick={sendOther}>
                Send
              </Button>
            </Flex>
          )}
        </Flex>
      )}
    </Flex>
  );
}
