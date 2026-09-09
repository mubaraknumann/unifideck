import { FC, ReactNode, RefAttributes, useCallback, useRef } from "react";
import {
  ConfirmModal,
  Focusable,
  GamepadButton,
  type FocusableProps,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

interface Props {
  version: string;
  body: string;
  closeModal?: () => void;
}

/** Steam's nav element accepts more props than @decky/ui types.
 *
 *  Both of the ones used below were verified against the live bundle:
 *  the component `@decky/ui` resolves `Focusable` to routes every
 *  unrecognised prop through a splitter that classifies
 *  `focusableIfEmpty` as a nav option and `onGamepadDirection` as a
 *  gamepad event, then binds it. `FocusableProps` only extends
 *  `HTMLAttributes`, so they need declaring to get past tsc. */
interface ScrollableFocusableProps extends FocusableProps {
  /** Makes a container a real focus target even though it holds nothing
   *  focusable — which is the whole situation here: release notes are
   *  prose, with no buttons or rows for the D-pad to step between. */
  focusableIfEmpty?: boolean;
  /** Return true to consume the direction press. */
  onGamepadDirection?: (e: CustomEvent) => boolean | void;
}

const ScrollableFocusable = Focusable as FC<
  ScrollableFocusableProps & RefAttributes<HTMLDivElement>
>;

/** Fraction of the viewport moved per D-pad press. 60 is what Steam's
 *  own EULA scroller uses, so this matches the feel of the rest of the
 *  UI rather than inventing a step size. */
const SCROLL_STEP_PERCENT = 60;

const parseInline = (text: string) => {
  const boldParts = text.split("**");
  const result: ReactNode[] = [];

  boldParts.forEach((part, boldIdx) => {
    const isBold = boldIdx % 2 !== 0;

    // Split by backticks for inline code styling
    const codeParts = part.split("`");
    const parsedCodeParts = codeParts.map((subPart, codeIdx) => {
      const isCode = codeIdx % 2 !== 0;
      if (isCode) {
        return (
          <code
            key={`${boldIdx}-${codeIdx}`}
            style={{
              background: "rgba(255, 255, 255, 0.12)",
              padding: "2px 6px",
              borderRadius: "4px",
              fontFamily: "monospace",
              fontSize: "0.9em",
              color: "#f59e0b",
            }}
          >
            {subPart}
          </code>
        );
      }
      return subPart;
    });

    if (isBold) {
      result.push(
        <strong
          key={boldIdx}
          style={{
            color: "#38bdf8",
            fontWeight: "bold",
          }}
        >
          {parsedCodeParts}
        </strong>,
      );
    } else {
      result.push(...parsedCodeParts);
    }
  });

  return result;
};

const parseMarkdown = (text: string, t: TFunction) => {
  if (!text) {
    return (
      <div style={{ opacity: 0.6, fontStyle: "italic", padding: "10px 0" }}>
        {t("updater.noReleaseNotes", {
          defaultValue: "No release notes available.",
        })}
      </div>
    );
  }

  const lines = text.split("\n");
  return lines.map((line, idx) => {
    const trimmed = line.trim();

    // Headers
    if (trimmed.startsWith("### ")) {
      return (
        <h3
          key={idx}
          style={{
            margin: "12px 0 6px 0",
            fontSize: "14px",
            fontWeight: "bold",
            color: "#ffffff",
          }}
        >
          {parseInline(trimmed.slice(4))}
        </h3>
      );
    }
    if (trimmed.startsWith("## ")) {
      return (
        <h2
          key={idx}
          style={{
            margin: "16px 0 8px 0",
            fontSize: "16px",
            fontWeight: "bold",
            color: "#ffffff",
            borderBottom: "1px solid rgba(255,255,255,0.1)",
            paddingBottom: "4px",
          }}
        >
          {parseInline(trimmed.slice(3))}
        </h2>
      );
    }
    if (trimmed.startsWith("# ")) {
      return (
        <h1
          key={idx}
          style={{
            margin: "18px 0 10px 0",
            fontSize: "18px",
            fontWeight: "bold",
            color: "#ffffff",
            borderBottom: "1px solid rgba(255,255,255,0.15)",
            paddingBottom: "6px",
          }}
        >
          {parseInline(trimmed.slice(2))}
        </h1>
      );
    }

    // Lists
    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      return (
        <li
          key={idx}
          style={{
            marginInlineStart: "20px",
            marginBottom: "6px",
            listStyleType: "disc",
            lineHeight: "1.4",
          }}
        >
          {parseInline(trimmed.slice(2))}
        </li>
      );
    }

    // Empty space/paragraph division
    if (trimmed === "") {
      return <div key={idx} style={{ height: "6px" }} />;
    }

    // Paragraph
    return (
      <p
        key={idx}
        style={{
          margin: "4px 0",
          lineHeight: "1.4",
        }}
      >
        {parseInline(line)}
      </p>
    );
  });
};

export const ReleaseNotesModal: FC<Props> = ({ version, body, closeModal }) => {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);

  // Mirrors Steam's EULA scroller: page the viewport on up/down, but
  // DON'T consume the press once we're at that end — so a second press
  // at the bottom hands focus back to the OK button instead of trapping
  // the user inside the notes.
  const onGamepadDirection = useCallback((e: CustomEvent): boolean => {
    const el = scrollRef.current;
    if (!el) return false;
    const step = (el.clientHeight * SCROLL_STEP_PERCENT) / 100;
    const button = (e.detail as { button?: number } | undefined)?.button;

    if (button === GamepadButton.DIR_UP) {
      if (el.scrollTop <= 0) return false;
      el.scrollBy({ top: -step, behavior: "smooth" });
    } else if (button === GamepadButton.DIR_DOWN) {
      // Sub-pixel layout means scrollTop + clientHeight can land just
      // shy of scrollHeight at the very bottom; the 1px slack stops the
      // last press being swallowed with nothing left to scroll.
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 1) return false;
      el.scrollBy({ top: step, behavior: "smooth" });
    } else {
      return false;
    }

    e.stopPropagation();
    return true;
  }, []);

  return (
    <ConfirmModal
      strTitle={t("updater.modalTitle", {
        version,
        defaultValue: `UNIFIDECK v${version} — Release Notes`,
      })}
      strOKButtonText={t("common.ok", { defaultValue: "OK" })}
      onOK={closeModal}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      {/* A plain scrolling div is unreachable with a controller: the
          D-pad moves focus between focusable elements, and prose has
          none, so the notes could only ever be read by touch or mouse.
          focusableIfEmpty makes this container itself the focus target
          and onGamepadDirection turns up/down into scrolling. The focus
          ring is left on deliberately, so it's visible that the region
          can be entered. */}
      <ScrollableFocusable
        ref={scrollRef}
        focusableIfEmpty
        onGamepadDirection={onGamepadDirection}
        style={{
          maxHeight: "320px",
          overflowY: "auto",
          padding: "12px 16px",
          background: "rgba(0, 0, 0, 0.25)",
          borderRadius: "6px",
          marginTop: "12px",
          fontSize: "13px",
          color: "#e5e7eb",
          fontFamily:
            'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        }}
      >
        {parseMarkdown(body, t)}
      </ScrollableFocusable>
    </ConfirmModal>
  );
};
