/**
 * pending-signin-store — a sign-in that is waiting for the user.
 *
 * The Microsoft device-code sign-in (xbox.com's client) runs in the Edge
 * auth window but finishes on the backend: the backend polls Microsoft until
 * the user presses "Allow access", so the flow outlives the window. This
 * store holds what the QAM shows meanwhile: the code (already filled in on
 * the page, shown here in case the window was closed), when it expires, and
 * whether the window is still open.
 *
 * Written only by `AuthDispatcher`; read by the QAM store rows.
 */
import { useSyncExternalStore } from "react";
import type { StoreId } from "../types/api";

/** One sign-in waiting for the user's approval. */
export interface PendingSignIn {
  store: StoreId;
  userCode: string;
  verificationUri: string;
  /** Epoch ms after which Microsoft no longer accepts the code. */
  expiresAt: number;
  /** False once the auth window has closed; the backend keeps waiting. */
  windowOpen: boolean;
}

type Listener = () => void;

class PendingSignInStore {
  private snapshot: ReadonlyMap<StoreId, PendingSignIn> = new Map();
  private listeners = new Set<Listener>();

  set(entry: PendingSignIn): void {
    this.commit(new Map(this.snapshot).set(entry.store, entry));
  }

  update(store: StoreId, patch: Partial<PendingSignIn>): void {
    const current = this.snapshot.get(store);
    if (current) this.set({ ...current, ...patch, store });
  }

  clear(store: StoreId): void {
    if (!this.snapshot.has(store)) return;
    const next = new Map(this.snapshot);
    next.delete(store);
    this.commit(next);
  }

  get(store: StoreId): PendingSignIn | undefined {
    return this.snapshot.get(store);
  }

  /** Arrow members: `useSyncExternalStore` calls these unbound. */
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): ReadonlyMap<StoreId, PendingSignIn> => this.snapshot;

  private commit(next: ReadonlyMap<StoreId, PendingSignIn>): void {
    this.snapshot = next;
    for (const listener of this.listeners) listener();
  }
}

/** Singleton — one per plugin load, like the other boot-time stores. */
export const pendingSignIns = new PendingSignInStore();

/** The sign-in `store` is waiting on, if any. */
export function usePendingSignIn(store: StoreId): PendingSignIn | undefined {
  return useSyncExternalStore(
    pendingSignIns.subscribe,
    pendingSignIns.getSnapshot,
  ).get(store);
}
