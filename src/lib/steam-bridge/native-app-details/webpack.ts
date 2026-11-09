/**
 * Find Steam components and CSS-module class maps by SHAPE.
 *
 * Minified export names and module ids change with every Steam build, so
 * nothing here is looked up by name. A component is found by strings in
 * its module's factory source (immune to runtime patching of the exports),
 * and a class map by the set of readable keys it carries.
 */

interface WebpackRequire {
  (id: string): unknown;
  m: Record<string, unknown>;
}

let cachedRequire: WebpackRequire | null = null;

/** Steam's webpack `require`, borrowed by pushing a throwaway chunk. */
export function getWebpackRequire(): WebpackRequire | null {
  if (cachedRequire) return cachedRequire;
  const w = window as unknown as { webpackChunksteamui?: unknown[] };
  if (!Array.isArray(w.webpackChunksteamui)) return null;
  let req: WebpackRequire | undefined;
  try {
    w.webpackChunksteamui.push([
      [`unifideck_native_details_${Math.random()}`],
      {},
      (r: WebpackRequire) => {
        req = r;
      },
    ] as never);
  } catch {
    return null;
  }
  cachedRequire = req && req.m ? req : null;
  return cachedRequire;
}

function factorySource(req: WebpackRequire, id: string): string {
  try {
    return String(req.m[id]);
  } catch {
    return "";
  }
}

function exportsOf(req: WebpackRequire, id: string): Record<string, unknown> {
  try {
    const e = req(id);
    return e && typeof e === "object" ? (e as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function valuesOf(exports: Record<string, unknown>): unknown[] {
  const out: unknown[] = [];
  for (const key of Object.keys(exports)) {
    try {
      out.push(exports[key]);
    } catch {
      /* a throwing getter: skip it */
    }
  }
  return out;
}

/**
 * The first export, in a module whose factory source contains every
 * needle, that `pick` accepts. `null` when no module matches.
 */
export function findExportInModule<T>(
  needles: readonly string[],
  pick: (value: unknown) => value is T,
): T | null {
  const req = getWebpackRequire();
  if (!req) return null;
  for (const id of Object.keys(req.m)) {
    const src = factorySource(req, id);
    if (!needles.every((n) => src.includes(n))) continue;
    const found = valuesOf(exportsOf(req, id)).find(pick);
    if (found !== undefined) return found;
  }
  return null;
}

/**
 * A CSS-module class map carrying every key in `keys`, or `{}`. With
 * `exact`, the map must carry those keys and no others (for small, generic
 * key sets such as `Container` / `Icon` / `Label`).
 */
export function findClassMap(
  keys: readonly string[],
  exact = false,
): Record<string, string> {
  const req = getWebpackRequire();
  if (!req) return {};
  for (const id of Object.keys(req.m)) {
    const exports = exportsOf(req, id);
    if (!keys.every((k) => typeof exports[k] === "string")) continue;
    if (exact && Object.keys(exports).length !== keys.length) continue;
    return exports as Record<string, string>;
  }
  return {};
}

export const REACT_FORWARD_REF = Symbol.for("react.forward_ref");
export const REACT_MEMO = Symbol.for("react.memo");

/** A `React.forwardRef` object: its `render` is read on every render. */
export interface ForwardRefObject {
  $$typeof: symbol;
  render: (props: Record<string, unknown>, ref: unknown) => unknown;
}

/** A `React.memo` object over a function component. */
export interface MemoObject {
  $$typeof: symbol;
  type: (props: Record<string, unknown>, ref: unknown) => unknown;
}

export function isForwardRef(v: unknown): v is ForwardRefObject {
  const o = v as ForwardRefObject | null;
  return (
    !!o && o.$$typeof === REACT_FORWARD_REF && typeof o.render === "function"
  );
}

export function isMemo(v: unknown): v is MemoObject {
  const o = v as MemoObject | null;
  return !!o && o.$$typeof === REACT_MEMO && typeof o.type === "function";
}

/** A class component constructor. */
export function isClassComponent(
  v: unknown,
): v is { prototype: Record<string, unknown> } {
  return (
    typeof v === "function" &&
    !!(v as { prototype?: { isReactComponent?: unknown } }).prototype
      ?.isReactComponent
  );
}
