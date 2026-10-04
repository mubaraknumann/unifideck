/**
 * Store logos as plain SVG data, for the Steam Store ownership ribbon.
 *
 * The ribbon renders inside the store page, where there is no React, so the
 * logos travel as data: `{ viewBox, nodes: [{ tag, attrs, children? }] }`.
 * The same glyph components `<StoreIcon>` renders are the source, read by
 * calling each component outside a render. React-icons glyphs return an
 * `IconBase` element whose `props.attr` carries the viewBox and whose
 * children are the shapes; `GameVaultIcon` returns an `<svg>` element
 * directly. Neither runs a hook, so no render is needed.
 *
 * This side converts every string/number prop to its SVG attribute name and
 * filters nothing else: the backend's allowlist
 * (`rpc/mixins/_store_ribbon_icons.py`) is the one authority on which tags
 * and attributes reach the page, so there is no second list here to drift.
 */
import { storeGlyph } from "../../components/shared/StoreIcon";
import { STORE_VISUALS } from "../../types/store";
import type { StoreId } from "../../types/api";

export interface IconNode {
  tag: string;
  attrs: Record<string, string>;
  children?: IconNode[];
}

export interface IconSpec {
  viewBox: string;
  nodes: IconNode[];
}

/** React-only props that are never SVG attributes. */
const SKIPPED_PROPS = new Set(["children", "key", "ref", "style", "className"]);
const MAX_DEPTH = 3;

interface ElementLike {
  type?: unknown;
  props?: Record<string, unknown>;
}

/** `fillRule` → `fill-rule`; already-kebab names pass through. */
function toAttrName(prop: string): string {
  return prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

function toNodes(children: unknown, depth: number): IconNode[] {
  if (depth > MAX_DEPTH) return [];
  const list = (Array.isArray(children) ? children : [children]).flat(
    MAX_DEPTH,
  );
  const nodes: IconNode[] = [];
  for (const child of list) {
    const { type, props } = (child ?? {}) as ElementLike;
    if (typeof type !== "string" || !props) continue;
    const attrs: Record<string, string> = {};
    for (const [prop, value] of Object.entries(props)) {
      if (SKIPPED_PROPS.has(prop)) continue;
      if (typeof value === "string" || typeof value === "number") {
        attrs[toAttrName(prop)] = String(value);
      }
    }
    const node: IconNode = { tag: type, attrs };
    const kids = toNodes(props.children, depth + 1);
    if (kids.length) node.children = kids;
    nodes.push(node);
  }
  return nodes;
}

/** The SVG data inside an icon element, or null when it has none. */
export function iconSpecFromElement(element: unknown): IconSpec | null {
  const props = (element as ElementLike | null)?.props;
  if (!props) return null;
  const attr = props.attr as Record<string, unknown> | undefined;
  const viewBox = attr?.viewBox ?? props.viewBox;
  if (typeof viewBox !== "string") return null;
  const nodes = toNodes(props.children, 0);
  return nodes.length ? { viewBox, nodes } : null;
}

let cached: Record<string, IconSpec> | null = null;

/** Every store's logo as SVG data. Static, so computed once. */
export function storeIconSpecs(): Record<string, IconSpec> {
  if (cached) return cached;
  const specs: Record<string, IconSpec> = {};
  for (const store of Object.keys(STORE_VISUALS) as StoreId[]) {
    try {
      const spec = iconSpecFromElement(storeGlyph(store)({}));
      if (spec) specs[store] = spec;
    } catch {
      /* a glyph that cannot be read keeps the ribbon's plain dot */
    }
  }
  cached = specs;
  return specs;
}
