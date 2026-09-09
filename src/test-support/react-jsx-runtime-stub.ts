/**
 * Minimal stand-in for React's automatic JSX runtime under vitest.
 *
 * `tsconfig.json` sets `"jsx": "react-jsx"`, so esbuild rewrites JSX in a
 * `.tsx` module to imports from `react/jsx-runtime` — which does not exist
 * here, because React is peer-provided by the Steam webview (SP_REACT) and is
 * deliberately absent from node_modules. Aliased alongside `react-stub.ts`.
 *
 * These return plain descriptor objects rather than React elements. That is
 * enough to *import* a `.tsx` module and unit-test the logic in it; it is not
 * enough to render anything, and no test should try.
 */
type Descriptor = Record<string, unknown>;

/** One child, or none. */
function jsx(type: unknown, props: unknown, key?: unknown): Descriptor {
  return { type, props, key };
}

/** Several children — same shape; the runtime distinguishes, we need not. */
const jsxs = jsx;

/** Development build's variant, with source/self args we discard. */
function jsxDEV(
  type: unknown,
  props: unknown,
  key?: unknown,
  _isStaticChildren?: boolean,
  _source?: unknown,
  _self?: unknown,
): Descriptor {
  return jsx(type, props, key);
}

const Fragment = Symbol.for("react.fragment");

export { jsx, jsxs, jsxDEV, Fragment };
