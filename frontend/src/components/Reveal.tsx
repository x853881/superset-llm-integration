import React, { useLayoutEffect, useRef } from 'react';

/** How long a result takes to roll open. */
const REVEAL_MS = 450;

/**
 * A result that rolls open downwards as it arrives - its height from nothing to its own, fading in -
 * rather than appearing in one piece, so what's under it is eased down instead of jumping. Only
 * when `active` (a call that has just landed): shown again later, say on coming back to its tab, it
 * is simply there.
 */
export function Reveal({ active, children }: { active: boolean; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!active || !element) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const height = element.offsetHeight;
    element.style.overflow = 'hidden';
    const animation = element.animate(
      [
        { height: '0px', opacity: 0 },
        { height: `${height}px`, opacity: 1 },
      ],
      { duration: REVEAL_MS, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' },
    );
    const done = () => {
      element.style.overflow = '';
    };
    animation.onfinish = done;
    animation.oncancel = done;
    // Only as it first arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div ref={ref}>{children}</div>;
}

/** Whether a call that settled at `finishedAt` has only just landed - so its result is revealed
 * rather than simply shown. */
export function justLanded(finishedAt: number | null | undefined): boolean {
  return finishedAt !== null && finishedAt !== undefined && Date.now() - finishedAt < 1500;
}
