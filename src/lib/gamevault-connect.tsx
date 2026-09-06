/**
 * GameVault sign-in — the modal chain, and the one promise it settles.
 *
 * GameVault is the only store whose sign-in is a form rather than a browser
 * OAuth or a Steam auth shortcut, so it takes the modal route before
 * `AuthDispatcher` is ever asked (the dispatcher coordinates a handshake
 * that does not exist here, and has no case for a credential form).
 *
 * With two modes there are now up to two modals in a row — the chooser, then
 * one of two forms — and either can be dismissed. That is what this module
 * exists for: `useStoreAuth.connect` must settle exactly once on every path,
 * or the Sign In button spins forever. The `settle` guard plus the explicit
 * `showModal` handles are the whole discipline, copied from
 * `pickStorageForInstall`; Steam's modal manager overwrites the injected
 * `closeModal` prop, so a flow that routes through it loses its callback.
 */
import { call } from "@decky/api";
import { showModal } from "@decky/ui";

import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import {
  GameVaultConnectModal,
  type GameVaultMode,
} from "../components/modals/GameVaultConnectModal";
import { GameVaultCredentialsModal } from "../components/modals/GameVaultCredentialsModal";
import { GameVaultLocalVaultModal } from "../components/modals/GameVaultLocalVaultModal";
import type { AuthResult } from "../types/api";

const STORE = "gamevault" as const;

export interface GameVaultConnectHandlers {
  /** Toggle the Sign In button's busy state while an RPC is in flight. */
  setBusy: (busy: boolean) => void;
}

/**
 * Drive the GameVault sign-in flow.
 *
 * Resolves with the `AuthResult` once a form succeeds, or `null` if the user
 * dismissed any step. Never rejects: a failure comes back as
 * `{ success: false, error }` so the caller has one shape to report.
 */
export function connectGameVault({
  setBusy,
}: GameVaultConnectHandlers): Promise<AuthResult | null> {
  return new Promise<AuthResult | null>((resolve) => {
    let settled = false;
    const settle = (result: AuthResult | null) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    /**
     * Run one connect RPC.
     *
     * Throws on failure so the form catches it and shows the message
     * inline; the promise stays open until the user succeeds or dismisses.
     */
    const runConnect = async (route: string, args: unknown[]) => {
      setBusy(true);
      try {
        const raw = await call<unknown[], unknown>(route, ...args);
        const result = unwrapRpcEnvelope<AuthResult>(raw, {
          route,
          throwing: false,
        });
        if (!result?.success) {
          throw new Error(result?.error ?? "connection_failed");
        }
        return { ...result, success: true, store: STORE } as AuthResult;
      } finally {
        setBusy(false);
      }
    };

    const openRemoteForm = () => {
      const handle = showModal(
        <GameVaultCredentialsModal
          onCancel={() => {
            handle?.Close();
            settle(null);
          }}
          onSubmit={async (
            serverUrl,
            username,
            password,
            verifySsl,
            downloadDir,
          ) => {
            const result = await runConnect(rpcRoutes.connectGamevault, [
              serverUrl,
              username,
              password,
              verifySsl,
              downloadDir,
            ]);
            handle?.Close();
            settle(result);
          }}
        />,
      );
    };

    const openLocalForm = () => {
      const handle = showModal(
        <GameVaultLocalVaultModal
          onCancel={() => {
            handle?.Close();
            settle(null);
          }}
          onSubmit={async (vaultDir) => {
            const result = await runConnect(rpcRoutes.connectGamevaultLocal, [
              vaultDir,
            ]);
            handle?.Close();
            settle(result);
          }}
        />,
      );
    };

    const chooser = showModal(
      <GameVaultConnectModal
        onCancel={() => {
          chooser?.Close();
          settle(null);
        }}
        onPick={(mode: GameVaultMode) => {
          // Close the chooser before opening the form, or it stays on the
          // modal stack behind it. Deliberately does NOT settle: the flow
          // is advancing, not ending.
          chooser?.Close();
          if (mode === "local") openLocalForm();
          else openRemoteForm();
        }}
      />,
    );
  });
}
