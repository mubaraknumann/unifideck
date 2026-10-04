/**
 * QamTabLabel — a Quick-Access tab title that scrolls when it is
 * too long for its half of the tab row and the tab has focus.
 *
 * The longest labels (French "Téléchargements") do not fit a
 * 300px QAM split in two, and are clipped. Steam's own `Marquee`
 * (what scrolls long game titles) only moves when its content
 * overflows, so short labels stay still and centred.
 *
 * Focus is read off the tab element Steam renders around us
 * (`role="tab"`): its `.gpfocus` class for the controller ring,
 * plus DOM focus for mouse and touch. Steam's `Tab` exposes no
 * focus callback we could pass in.
 */
import { FC, useEffect, useRef, useState } from "react";
import { Marquee } from "@decky/ui";

/** Seconds a focused label holds still before it starts scrolling. */
const MARQUEE_DELAY_S = 1;

export const QamTabLabel: FC<{ text: string }> = ({ text }) => {
  const ref = useRef<HTMLDivElement>(null);
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    const tab = ref.current?.closest<HTMLElement>('[role="tab"]');
    if (!tab) return;
    let domFocus = false;
    const sync = (): void =>
      setFocused(domFocus || tab.classList.contains("gpfocus"));
    const onIn = (): void => {
      domFocus = true;
      sync();
    };
    const onOut = (): void => {
      domFocus = false;
      sync();
    };
    const observer = new MutationObserver(sync);
    observer.observe(tab, { attributes: true, attributeFilter: ["class"] });
    tab.addEventListener("focusin", onIn);
    tab.addEventListener("focusout", onOut);
    sync();
    return () => {
      observer.disconnect();
      tab.removeEventListener("focusin", onIn);
      tab.removeEventListener("focusout", onOut);
    };
  }, []);

  // `Marquee` is looked up in Steam's bundle at runtime; plain text keeps
  // the tab usable (clipped, with an ellipsis) if Steam ever renames it.
  return (
    <div ref={ref} style={{ minWidth: 0, width: "100%" }}>
      {Marquee ? (
        <Marquee play={focused} center delay={MARQUEE_DELAY_S}>
          {text}
        </Marquee>
      ) : (
        <span
          style={{
            display: "block",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {text}
        </span>
      )}
    </div>
  );
};
