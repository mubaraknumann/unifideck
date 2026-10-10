/**
 * ExpandableRow — one compact row that opens to show its actions.
 *
 * Collapsed, a row is a single gamepad target: icon, name, one line of
 * status and a chevron fixed at the row's end. A (or a tap) opens it and
 * leaves focus on the row, so D-pad down then walks into the actions.
 * A again on the row closes it; B from inside the actions closes it too
 * and puts focus back on the row — without that, focus would be lost
 * with the button that held it and Steam would send it to the top of
 * the panel.
 *
 * Which row is open is owned by the parent (one at a time); styles come
 * from `storeConnections.css.ts`.
 */
import { FC, ReactNode, useEffect, useRef } from "react";
import { Focusable } from "@decky/ui";
import { FaChevronDown } from "react-icons/fa";
import type { StoreStatusLine } from "./storeStatus";

interface Props {
  icon: ReactNode;
  label: string;
  status: StoreStatusLine;
  expanded: boolean;
  onToggle: () => void;
  /** Action buttons, shown while expanded. */
  children: ReactNode;
}

export const ExpandableRow: FC<Props> = ({
  icon,
  label,
  status,
  expanded,
  onToggle,
  children,
}) => {
  const headerRef = useRef<HTMLDivElement>(null);
  const actionsRef = useRef<HTMLDivElement>(null);
  const toneClass = status.tone === "error" ? " error" : "";

  // Opening a row near the bottom of the panel would otherwise leave its
  // buttons below the fold.
  useEffect(() => {
    if (expanded) {
      actionsRef.current?.scrollIntoView?.({ block: "nearest" });
    }
  }, [expanded]);

  const collapseFromActions = (e: CustomEvent): void => {
    // Handled here: B closes this row instead of the whole panel.
    e.stopPropagation();
    onToggle();
    // After the buttons unmount, so the row is the only thing left to take
    // focus.
    window.requestAnimationFrame(() => headerRef.current?.focus());
  };

  return (
    <div className={`unifideck-store-row${expanded ? " expanded" : ""}`}>
      <Focusable
        ref={headerRef}
        className="unifideck-store-row-header"
        onActivate={onToggle}
        aria-expanded={expanded}
      >
        {icon}
        <span className="unifideck-store-row-name">{label}</span>
        <span className={`unifideck-store-row-status${toneClass}`}>
          {status.text}
        </span>
        <span className="unifideck-store-row-chevron" aria-hidden>
          <FaChevronDown size={12} />
        </span>
      </Focusable>
      {expanded && (
        <Focusable
          ref={actionsRef}
          flow-children="column"
          className="unifideck-store-row-actions"
          onCancel={collapseFromActions}
        >
          {children}
          {status.detail && (
            <div className={`unifideck-store-row-detail${toneClass}`}>
              {status.detail}
            </div>
          )}
        </Focusable>
      )}
    </div>
  );
};
