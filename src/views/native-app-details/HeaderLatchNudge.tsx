/**
 * HeaderLatchNudge: unstick Steam's tab header after a tab mounts.
 *
 * Steam decides whether its tab header sits "under the page header" from
 * one measurement, re-checked only on scroll. Measured before the hero art
 * and our content have laid out, the header latched over the play bar
 * (seen live 2026-11-02, fixed by one scroll event). Rendered inside each
 * of our tabs: it dispatches one scroll event on the page's scroll
 * container shortly after mount, and once more after late content (our
 * achievements) has had time to arrive. Renders an empty, hidden span.
 */
import { FC, useEffect, useRef } from "react";

/** Delays (ms) after mount at which the page is nudged. */
const NUDGE_AFTER_MS = [250, 1000];

/** Dispatch one scroll event on the nearest scrolling ancestor. */
function nudgeScrollContainer(from: HTMLElement): void {
  const view = from.ownerDocument.defaultView;
  for (let el = from.parentElement; el; el = el.parentElement) {
    const overflow = view?.getComputedStyle(el).overflowY;
    if (
      (overflow === "auto" || overflow === "scroll") &&
      el.scrollHeight > el.clientHeight
    ) {
      el.dispatchEvent(new Event("scroll"));
      return;
    }
  }
}

export const HeaderLatchNudge: FC = () => {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const timers = NUDGE_AFTER_MS.map((ms) =>
      window.setTimeout(() => {
        if (ref.current) nudgeScrollContainer(ref.current);
      }, ms),
    );
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, []);
  return <span ref={ref} style={{ display: "none" }} />;
};
