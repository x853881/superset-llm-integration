import React, { useLayoutEffect, useRef, useState } from 'react';
import { Button, Flex, Typography } from 'antd';
import PlusOutlined from '@ant-design/icons/PlusOutlined';
import PartitionOutlined from '@ant-design/icons/PartitionOutlined';
import { useTokens } from '../../components/primitives';
import { AskComposer } from '../../components/AskComposer';
import { FlowingResponse } from './FlowingResponse';
import type { ConversationView } from '../layout';

/** The SQL Lab tabs something has been asked in, for as long as the page is open - where the box
 * sits (top or foot) is kept per tab. */
const STARTED = new Set<string>();

/** The box's drop from the top of the tab to its foot: an ease-out that settles gently rather than
 * stopping dead (easeOutQuint), long enough to be followed, short enough not to be waited on. */
const DROP = { duration: 560, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' };

/**
 * Alpha's Ask tab: prompting, not a chat. Before anything is asked, Alpha's ask box (`AskComposer`)
 * at the top of the tab, as it always was; the first prompt drops the box to the foot of the panel,
 * and the latest response - and only that, drawn as it always is - fills the space above it. What
 * was asked isn't kept on screen. The first prompt writes SQL; each one after changes the SQL
 * written so far (a refinement).
 */
export function Conversation({ tabKey, conversation }: { tabKey: string; conversation: ConversationView }) {
  const token = useTokens();
  const [started, setStartedState] = useState(() => STARTED.has(tabKey));
  const [draft, setDraft] = useState('');
  const box = useRef<HTMLDivElement>(null);
  const response = useRef<HTMLDivElement>(null);
  const lastBoxTop = useRef<number | null>(null);
  const wasStarted = useRef(started);

  // A different SQL Lab tab has its own.
  const shownFor = useRef(tabKey);
  if (shownFor.current !== tabKey) {
    shownFor.current = tabKey;
    const theirs = STARTED.has(tabKey);
    setStartedState(theirs);
    wasStarted.current = theirs;
    lastBoxTop.current = null;
  }
  const setStarted = (next: boolean) => {
    if (next) STARTED.add(tabKey);
    else STARTED.delete(tabKey);
    setStartedState(next);
  };

  // FLIP: when the box moves between the top and the foot, start it where it was and let it ease
  // into its new place. Measured on every commit, so "where it was" is always the last place it
  // was drawn.
  useLayoutEffect(() => {
    const element = box.current;
    if (!element) return;
    const top = element.getBoundingClientRect().top;
    const moved = wasStarted.current !== started;
    wasStarted.current = started;
    const from = lastBoxTop.current;
    lastBoxTop.current = top;
    if (!moved || from === null || from === top) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    element.animate([{ transform: `translateY(${from - top}px)` }, { transform: 'translateY(0)' }], DROP);
    response.current?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 360, delay: 200, easing: 'ease-out', fill: 'backwards' });
  });

  /** Sends `text` - typed, a refinement the latest response suggested, or an answer to a question
   * it asked. */
  const prompt = (text: string) => {
    if (!text || !conversation.canSend) return;
    // A refinement changes the SQL written so far - so the first prompt, or one with no SQL to
    // change, writes SQL from scratch.
    if (started && conversation.hasSql) conversation.followUp(text);
    else conversation.ask(text);
    setStarted(true);
  };
  const [refused, setRefused] = useState(false);
  const send = () => {
    if (!draft.trim()) return;
    // Anything else waits - and says so, rather than Enter doing nothing.
    if (!conversation.canSend) {
      setRefused(true);
      return;
    }
    prompt(draft.trim());
    setDraft('');
  };

  // The box stays the second child either way, so it isn't remounted - and keeps its focus - as it
  // moves from the top of the tab to its foot.
  return (
    <Flex vertical style={{ flex: 1, minHeight: 0, padding: `0 ${token.paddingSM}px ${token.paddingSM}px` }}>
      {started ? (
        // Scrolled as on Alpha's other tabs (`FlowingResponse`).
        <FlowingResponse running={conversation.sending} containerRef={response} style={{ padding: `${token.paddingSM}px 0` }}>
          {conversation.latest(prompt)}
        </FlowingResponse>
      ) : (
        <div />
      )}
      <div ref={box} style={{ flexShrink: 0, width: '100%' }}>
        <Flex vertical gap={token.marginXS}>
          {started && (
            <Button
              type="link"
              size="small"
              icon={<PlusOutlined />}
              disabled={conversation.sending}
              onClick={() => setStarted(false)}
              style={{ alignSelf: 'flex-start', padding: 0, height: 'auto', fontSize: token.fontSizeSM }}
            >
              New question
            </Button>
          )}
          {/* Alpha's own ask box, as it was. */}
          <AskComposer
            value={draft}
            onChange={text => {
              setDraft(text);
              setRefused(false);
            }}
            onSubmit={send}
            submitLabel="Ask"
            submitting={conversation.sending}
            submitDisabled={draft.trim() === '' || conversation.sending}
            onCancelEdit={null}
            showExamples={!started}
            submitOnEnter
            showLength={false}
            footerStart={
              <Button size="small" icon={<PartitionOutlined />} disabled={!conversation.hasSql} onClick={conversation.explainQuery}>
                Explain query
              </Button>
            }
          />
          {refused && conversation.waitingOn && (
            <Typography.Text type="secondary" style={{ fontSize: token.fontSizeSM }}>
              {conversation.waitingOn} - send this once it has finished.
            </Typography.Text>
          )}
        </Flex>
      </div>
      {!started && <div style={{ flex: 1 }} />}
    </Flex>
  );
}
