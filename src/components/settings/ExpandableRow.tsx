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
 * Which row is open is owned by the parent (one at a time), and so is
 * scroll: opening a row never scrolls the panel itself. `onToggle` hands
 * the parent this row's element so it can keep the tapped header still
 * while other rows close around it. Styles come from
 * `storeConnections.css.ts`.
 */
import { FC, ReactNode, useRef } from "react";
import { Focusable } from "@decky/ui";
import { FaChevronDown } from "react-icons/fa";
import type { StoreStatusLine } from "./storeStatus";

interface Props {
  icon: ReactNode;
  label: string;
  status: StoreStatusLine;
  expanded: boolean;
  /** Called with this row's element, so the parent can anchor its scroll. */
  onToggle: (row: HTMLElement | null) => void;
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
  const rootRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const toneClass = status.tone === "error" ? " error" : "";
  const toggle = (): void => onToggle(rootRef.current);

  const collapseFromActions = (e: CustomEvent): void => {
    // Handled here: B closes this row instead of the whole panel.
    e.stopPropagation();
    toggle();
    // After the buttons unmount, so the row is the only thing left to take
    // focus.
    window.requestAnimationFrame(() => headerRef.current?.focus());
  };

  return (
    <div
      ref={rootRef}
      className={`unifideck-store-row${expanded ? " expanded" : ""}`}
    >
      <Focusable
        ref={headerRef}
        className="unifideck-store-row-header"
        onActivate={toggle}
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
          flow-children="column"
          className="unifideck-store-row-actions"
          onCancel={collapseFromActions}
        >
          {/* Above the buttons, straight under the row it explains, where
              it cannot be missed. */}
          {status.detail && (
            <div className={`unifideck-store-row-detail${toneClass}`}>
              {status.detail}
            </div>
          )}
          {children}
        </Focusable>
      )}
    </div>
  );
};
