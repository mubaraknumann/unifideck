/**
 * Minimal stand-in for React under vitest. At runtime React is
 * peer-provided by the Steam webview (SP_REACT) and is deliberately
 * not installed in node_modules, so tests that import steam-bridge
 * modules resolve "react" to this stub via the vitest alias.
 */
/**
 * Synchronous `useMemo`: run the factory and return its value, ignoring the
 * dependency list. Enough to unit-test a hook whose body is a pure decision
 * over its inputs (`usePlaySection`), and honest about what it does not
 * cover — memoisation and re-render behaviour are not exercised here.
 */
function useMemo<T>(factory: () => T, _deps?: readonly unknown[]): T {
  return factory();
}

/**
 * `useState` that never re-renders: the setter mutates a cell the hook body
 * cannot observe again, because the body is only ever run once per call here.
 * Enough to unit-test a hook whose *effects* are the thing under test
 * (`useStoreAuth` firing the post-login sync) and honest about what it does
 * not cover: nothing that depends on reading state back after a set.
 */
function useState<T>(initial: T): [T, (next: T) => void] {
  let value = initial;
  return [
    value,
    (next: T) => {
      value = next;
    },
  ];
}

/** `useCallback`: hand the function straight back, deps ignored. */
function useCallback<T>(fn: T, _deps?: readonly unknown[]): T {
  return fn;
}

const React = {
  createElement: (
    type: unknown,
    props: unknown,
    ...children: unknown[]
  ): Record<string, unknown> => ({ type, props, children }),
  useMemo,
  useState,
  useCallback,
};

export { useMemo, useState, useCallback };
export default React;
