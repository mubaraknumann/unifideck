/**
 * Wire-shape helper for `call` mocks.
 *
 * Every backend RPC wrapped by `@auto_wrap_rpc_methods` answers with
 * `{success, error, data}` (`py_modules/unifideck/rpc/wrapper.py`). A
 * fixture that hands a reader the bare payload passes while production
 * loads nothing — that is exactly how `app-store-patcher` and
 * `protondb-cache` ran with empty caches for months. Build every mocked
 * RPC response through this so the fixture cannot drift from the wire.
 */
export function envelope<T>(
  data: T,
  ok = true,
  error: string | null = null,
): { success: boolean; error: string | null; data: T } {
  return { success: ok, error, data };
}
