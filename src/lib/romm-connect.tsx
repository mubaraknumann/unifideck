/**
 * RomM sign-in: the modal, the pairing poll, and the one promise it settles.
 *
 * `useStoreAuth.connect` has to settle exactly once on every path or the Sign
 * In button spins forever, so that discipline lives here: a `settle` guard, an
 * explicit `showModal` handle, and an attempt id the backend can cancel.
 *
 * Callbacks rather than the injected `closeModal` prop: Steam's modal manager
 * overwrites that prop, so a flow routing through it loses its callback. The
 * handle kept here is what closes the modal.
 *
 * The attempt id is what makes dismissal safe. A fresh id is taken before each
 * new attempt and the abandoned one is cancelled, so a late reply to an
 * attempt the user has left cannot be mistaken for the current one.
 */
import { call } from "@decky/api";
import { showModal } from "@decky/ui";

import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import {
  RomMConnectModal,
  type RomMConnectValues,
  type RomMPairing,
  type RomMVariant,
} from "../components/modals/RomMConnectModal";
import type { AuthResult } from "../types/api";

export interface RomMConnectHandlers {
  /** Toggle the Sign In button's busy state while an RPC is in flight. */
  setBusy: (busy: boolean) => void;
}

function newAttemptId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function isVariantList(value: unknown): value is RomMVariant[] {
  return (
    Array.isArray(value) &&
    value.every(
      (v) =>
        typeof v === "object" &&
        v !== null &&
        typeof v.platform_slug === "string" &&
        typeof v.fs_slug === "string" &&
        typeof v.display_name === "string",
    )
  );
}

function isPairing(value: unknown): value is RomMPairing {
  if (!value || typeof value !== "object") return false;
  const p = value as RomMPairing;
  return (
    typeof p.user_code === "string" &&
    typeof p.verification_url === "string" &&
    /^https?:\/\//.test(p.verification_url) &&
    Number.isFinite(p.expires_in) &&
    p.expires_in > 0 &&
    Number.isFinite(p.interval) &&
    p.interval > 0
  );
}

/**
 * Run the RomM connection flow, resolving with the auth result or null.
 *
 * `null` means the user dismissed the modal without committing anything. Every
 * other outcome is an `AuthResult`, including the failure envelope built when a
 * cancel could not be confirmed.
 */
export function connectRomM({
  setBusy,
}: RomMConnectHandlers): Promise<AuthResult | null> {
  return new Promise((resolve) => {
    let attemptId = newAttemptId();
    let settled = false;
    let dismissing = false;
    let started = false;
    const settle = (result: AuthResult | null) => {
      if (settled) return;
      settled = true;
      setBusy(false);
      resolve(result);
    };
    const rpc = async (route: string, ...args: unknown[]) => {
      const raw = await call<unknown[], unknown>(route, ...args);
      return unwrapRpcEnvelope<AuthResult>(raw, { route, throwing: false });
    };
    // Abandons an attempt that has to be restarted: a failed or malformed
    // reply, or a variant list with nothing to commit. A fresh id is taken
    // first, then a best-effort cancel is sent for the stale one, so a late
    // reply to the abandoned attempt cannot be accepted as this attempt's.
    const reset = async () => {
      const stale = attemptId;
      attemptId = newAttemptId();
      started = false;
      await rpc(rpcRoutes.cancelRommConnection, stale).catch(() => undefined);
    };
    // A dismissal is only "nothing was committed" once the backend confirms
    // it. An attempt with nothing to choose commits as soon as the variants
    // arrive, and the cancel that follows deletes that saved connection, so a
    // cancel that fails settles with an error rather than null.
    const dismiss = () => {
      if (settled || dismissing) return;
      dismissing = true;
      handle?.Close();
      if (!started) {
        settle(null);
        return;
      }
      void rpc(rpcRoutes.cancelRommConnection, attemptId).then(
        (result) =>
          settle(
            result?.success
              ? null
              : {
                  ...result,
                  success: false,
                  store: "romm",
                  error:
                    result?.error ?? "Could not cancel the RomM connection",
                },
          ),
        () =>
          settle({
            success: false,
            store: "romm",
            error: "Could not cancel the RomM connection",
          }),
      );
    };
    // A malformed variant list is a failure. An empty one is a real answer,
    // but there is nothing to commit, so the attempt is abandoned first and the
    // modal reports the empty list itself.
    const variantsFrom = async (result: AuthResult | null) => {
      const variants = result?.metadata?.variants;
      if (!isVariantList(variants)) throw new Error("connection_failed");
      if (variants.length === 0) await reset();
      return variants;
    };
    const onBegin = async (
      v: RomMConnectValues,
    ): Promise<RomMVariant[] | RomMPairing> => {
      started = true;
      setBusy(true);
      try {
        const result = await rpc(
          rpcRoutes.connectRomm,
          attemptId,
          v.serverUrl,
          v.token,
          v.verifySsl,
          v.downloadDir,
        );
        if (settled || dismissing) throw new Error("cancelled");
        if (!result?.success)
          throw new Error(result?.error ?? "connection_failed");
        if (result.action === "pair") {
          if (!isPairing(result.metadata)) throw new Error("connection_failed");
          return result.metadata;
        }
        return await variantsFrom(result);
      } catch (err) {
        if (!settled && !dismissing) await reset();
        throw err;
      } finally {
        setBusy(false);
      }
    };
    const onPoll = async (): Promise<RomMVariant[] | null> => {
      try {
        const result = await rpc(rpcRoutes.pollRommPairing, attemptId);
        if (settled || dismissing) throw new Error("cancelled");
        if (!result?.success)
          throw new Error(result?.error ?? "connection_failed");
        if (result.action === "pending") return null;
        return await variantsFrom(result);
      } catch (err) {
        if (!settled && !dismissing) await reset();
        throw err;
      }
    };
    const onComplete = async (excluded: string[]) => {
      setBusy(true);
      try {
        const result = await rpc(
          rpcRoutes.completeRommConnection,
          attemptId,
          excluded,
        );
        if (settled || dismissing) throw new Error("cancelled");
        if (!result?.success)
          throw new Error(result?.error ?? "connection_failed");
        handle?.Close();
        settle({ ...result, success: true, store: "romm" });
      } finally {
        setBusy(false);
      }
    };
    const handle = showModal(
      <RomMConnectModal
        onBegin={onBegin}
        onPoll={onPoll}
        onComplete={onComplete}
        onCancel={dismiss}
      />,
    );
  });
}
