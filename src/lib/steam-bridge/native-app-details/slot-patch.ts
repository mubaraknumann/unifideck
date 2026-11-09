/**
 * Reload-safe wrapping of a function slot on a Steam-owned object.
 *
 * The objects patched here (a `forwardRef`'s `render`, a `memo`'s `type`,
 * a class prototype's accessor) are webpack singletons. They outlive a
 * plugin reload, while this module's own variables do not. So the original
 * is stashed ON the owner, and a wrapper is branded, the same scheme as the
 * library tile patch: a fresh install after a reload finds the stash and
 * never wraps a wrapper.
 */

type AnyFn = (...args: never[]) => unknown;

const BRAND = "__unifideckWrapper";

function stashKey(key: string): string {
  return `__unifideckOrig_${key}`;
}

type Owner = Record<string, unknown>;

/**
 * Replace `owner[key]` with `makeWrapper(original)`. Returns a disposer
 * that restores the original, but only while our wrapper is still the
 * live value (a foreign patch layered on top is never clobbered).
 */
export function patchSlot<F extends AnyFn>(
  owner: object,
  key: string,
  makeWrapper: (original: F) => F,
): () => void {
  const o = owner as Owner;
  const current = o[key] as (F & { [BRAND]?: true }) | undefined;
  const stashed = o[stashKey(key)] as F | undefined;
  let original: F | undefined;
  if (typeof stashed === "function") original = stashed;
  else if (typeof current === "function" && !current[BRAND]) original = current;
  if (!original) {
    throw new Error(`cannot recover the original ${key}`);
  }
  const wrapper = makeWrapper(original) as F & { [BRAND]?: true };
  wrapper[BRAND] = true;
  o[stashKey(key)] = original;
  o[key] = wrapper;
  const restore = original;
  return () => {
    try {
      if (o[key] === wrapper) o[key] = restore;
      if (o[key] !== wrapper) delete o[stashKey(key)];
    } catch (e) {
      console.warn(`[Unifideck NativeDetails] unpatch of ${key} failed:`, e);
    }
  };
}

/**
 * Replace a prototype accessor's getter. `makeGetter` receives the original
 * getter. The original descriptor is stashed on the prototype so a reload
 * restores the real one, not a previous instance's wrapper.
 */
export function patchGetter(
  proto: object,
  key: string,
  makeGetter: (
    originalGet: (this: unknown) => unknown,
  ) => (this: unknown) => unknown,
): () => void {
  const p = proto as Owner;
  const stash = stashKey(`get_${key}`);
  const stashed = p[stash] as PropertyDescriptor | undefined;
  const live = Object.getOwnPropertyDescriptor(proto, key);
  const original = stashed ?? live;
  if (!original?.get || !original.configurable) {
    throw new Error(`${key} is not a configurable accessor`);
  }
  const getter = makeGetter(original.get);
  Object.defineProperty(p, stash, {
    value: original,
    configurable: true,
    enumerable: false,
    writable: true,
  });
  Object.defineProperty(proto, key, {
    configurable: true,
    enumerable: original.enumerable,
    get: getter,
  });
  return () => {
    try {
      const now = Object.getOwnPropertyDescriptor(proto, key);
      if (now?.get === getter) Object.defineProperty(proto, key, original);
      delete p[stash];
    } catch (e) {
      console.warn(`[Unifideck NativeDetails] unpatch of ${key} failed:`, e);
    }
  };
}
