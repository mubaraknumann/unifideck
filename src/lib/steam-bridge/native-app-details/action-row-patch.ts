/**
 * Steam's play-bar row (button · status and stats · icon buttons).
 *
 * The row is a `React.forwardRef` whose render builds
 * `ActionRow > [ActionButtonAndStatusPanel, StatusAndStats, AppButtons]`.
 * It is not exported, so it is found once at runtime: the action-button
 * host renders a hidden probe inside the row, and this walks the probe's
 * React fiber up to the forwardRef that renders `ActionButtonAndStatusPanel`
 * and `AppButtons`. Its `render` is then wrapped like any other forwardRef.
 *
 * For our app ids the wrapper swaps the stats element (the child carrying a
 * `statusPanelType` prop) for ours, and appends our icon buttons inside
 * `AppButtons`. Everything is a real React child, so gamepad focus moves
 * through it like through Steam's own buttons.
 */
import type { ReactElement } from "react";
import { isUnifideckAppId } from "./gate";
import { patchSlot } from "./slot-patch";
import { isForwardRef, type ForwardRefObject } from "./webpack";

export interface ActionRowProps {
  overview?: { appid?: number };
  details?: unknown;
  [key: string]: unknown;
}

export type ActionRowDecorator = (
  row: ReactElement,
  props: ActionRowProps,
  appId: number,
) => ReactElement;

interface Fiber {
  type?: unknown;
  return?: Fiber | null;
  memoizedProps?: Record<string, unknown> | null;
  stateNode?: { forceUpdate?: () => void } | null;
}

let unpatchRow: (() => void) | null = null;
let unpatchSection: (() => void) | null = null;
let decorator: ActionRowDecorator | null = null;
let sectionDecorator: ActionRowDecorator | null = null;

function fiberOf(node: Element): Fiber | null {
  const key = Object.keys(node).find((k) => k.startsWith("__reactFiber$"));
  return key ? (node as unknown as Record<string, Fiber>)[key] ?? null : null;
}

function renderSource(fiber: Fiber): string {
  if (!isForwardRef(fiber.type)) return "";
  try {
    return String(fiber.type.render);
  } catch {
    return "";
  }
}

function isActionRow(fiber: Fiber): boolean {
  if (!fiber.memoizedProps?.details) return false;
  const src = renderSource(fiber);
  return (
    src.includes("ActionButtonAndStatusPanel") && src.includes("AppButtons")
  );
}

/** `PlaySection`: the row's parent forwardRef, whose element is followed
 *  by Steam's own cloud-status line. */
function isPlaySection(fiber: Fiber): boolean {
  const src = renderSource(fiber);
  return src.includes(".PlaySection") && src.includes("onNav");
}

/** Wrap a forwardRef's render with `decorate` for our app ids. */
function wrapRender(
  type: ForwardRefObject,
  label: string,
  current: () => ActionRowDecorator | null,
): () => void {
  return patchSlot<ForwardRefObject["render"]>(
    type,
    "render",
    (original) =>
      function render(this: unknown, props, ref) {
        const out = original.call(this, props, ref) as ReactElement;
        const decorate = current();
        if (!decorate) return out;
        try {
          const p = props as ActionRowProps;
          const appId = p?.overview?.appid;
          return isUnifideckAppId(appId) ? decorate(out, p, appId) : out;
        } catch (e) {
          console.error(`[Unifideck NativeDetails] ${label} left stock:`, e);
          return out;
        }
      },
  );
}

/** Set the row decorator; it applies once the row is found. */
export function setActionRowDecorator(fn: ActionRowDecorator | null): void {
  decorator = fn;
}

/** Set the decorator for the `PlaySection` element (the row's parent). */
export function setPlaySectionDecorator(fn: ActionRowDecorator | null): void {
  sectionDecorator = fn;
}

/** Whether the `PlaySection` patch is live (else callers fall back to
 *  putting what belongs after it inside the row). */
export function isPlaySectionPatched(): boolean {
  return unpatchSection != null;
}

/**
 * Find the row (and its `PlaySection` parent) from a node rendered inside
 * it, and patch both. Idempotent: after the first success it returns at
 * once. Returns whether the row is patched.
 */
export function patchActionRowFrom(node: Element): boolean {
  if (unpatchRow) return true;
  if (!decorator) return false;
  let fiber = fiberOf(node);
  while (fiber && !isActionRow(fiber)) fiber = fiber.return ?? null;
  if (!fiber) return false;
  let section: Fiber | null = fiber.return ?? null;
  while (section && !isPlaySection(section)) section = section.return ?? null;
  try {
    if (section && sectionDecorator) {
      unpatchSection = wrapRender(
        section.type as ForwardRefObject,
        "play section",
        () => sectionDecorator,
      );
    }
    unpatchRow = wrapRender(
      fiber.type as ForwardRefObject,
      "play bar",
      () => decorator,
    );
  } catch (e) {
    console.error("[Unifideck NativeDetails] play bar patch failed:", e);
    unpatchSection?.();
    unpatchSection = null;
    return false;
  }
  // The row rendered once without us; re-render it through the patches.
  for (let up = (section ?? fiber).return; up; up = up.return ?? null) {
    if (typeof up.stateNode?.forceUpdate === "function") {
      up.stateNode.forceUpdate();
      break;
    }
  }
  return true;
}

export function unpatchActionRow(): void {
  unpatchRow?.();
  unpatchRow = null;
  unpatchSection?.();
  unpatchSection = null;
}
