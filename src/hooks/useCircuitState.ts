/**
 * Live circuit-breaker state for one game.
 *
 * After three failed launches inside the configured window the backend
 * refuses to launch. Until now that was **completely invisible and
 * unresettable**: `CIRCUIT_STATE_CHANGED` was polled via `WATCHED_EVENTS`
 * and dropped on the floor, and the two RPCs that reset it
 * (`clear_launch_failures`, `arm_circuit_bypass`) had no caller. So the user
 * pressed Play, got a flicker, landed back on the game page with no message,
 * no badge, and no way out short of waiting the window out — reading as "the
 * plugin randomly stopped launching my game". Audit register item 4a, which
 * the audit called the highest-value item left in its register.
 *
 * The payload keys below are the ones the emitter actually sends
 * (`services/launch_history/service.py`). The enum's docstring used to
 * document a different set — `game_key`, `state`, `recent_count`,
 * `failure_kinds` — none of which exists; a hook built from that docstring
 * would have read `undefined` for four of five fields. That was corrected in
 * the §1.3 pass specifically so this hook could be written from it.
 *
 * Note the state can only be trusted once **item 46** shipped: the breaker
 * could never clear on a successful launch, because its `GAME_STOPPED`
 * handler read an `rc` key no emitter sends. A badge built before that fix
 * would have shown failures that never went away.
 */
import { useCallback, useEffect, useState } from "react";
import { call } from "@decky/api";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { useEventBus } from "../api/event-bus-client";
import { Events } from "../types/events";

/** What `get_launch_failures` returns. */
interface LaunchFailuresPayload {
  failures: { kind?: string; error_code?: string; at?: number }[];
  circuit_open: boolean;
  fail_count: number;
}

export interface CircuitState {
  /** True while the breaker is refusing launches for this game. */
  open: boolean;
  /** Failures recorded inside the current window. */
  failureCount: number;
  /** Distinct failure kinds seen, for the tooltip. */
  kinds: string[];
  /** Wipe the failure history entirely. */
  reset: () => Promise<void>;
  /** Arm a one-shot bypass so the next launch goes through. */
  forceLaunch: () => Promise<void>;
}

const EMPTY: Omit<CircuitState, "reset" | "forceLaunch"> = {
  open: false,
  failureCount: 0,
  kinds: [],
};

export function useCircuitState(
  store: string | undefined | null,
  gameId: string | undefined | null,
): CircuitState {
  const gameKey = store && gameId ? `${store}:${gameId}` : null;
  const [state, setState] = useState(EMPTY);

  const refetch = useCallback(async () => {
    if (!gameKey) {
      setState(EMPTY);
      return;
    }
    try {
      const raw = await call<[string], unknown>(
        rpcRoutes.getLaunchFailures,
        gameKey,
      );
      const data = unwrapRpcEnvelope<LaunchFailuresPayload>(raw, {
        route: rpcRoutes.getLaunchFailures,
        throwing: false,
      });
      if (!data) {
        setState(EMPTY);
        return;
      }
      setState({
        open: data.circuit_open === true,
        failureCount: data.fail_count ?? 0,
        kinds: Array.from(
          new Set((data.failures ?? []).map((f) => f.kind).filter(Boolean)),
        ) as string[],
      });
    } catch {
      // A diagnostic badge must never break the Play page.
      setState(EMPTY);
    }
  }, [gameKey]);

  // Mount fetch: the event only fires on a *change*, so a game that was
  // already tripped before this page opened would otherwise show nothing.
  useEffect(() => {
    void refetch();
  }, [refetch]);

  useEventBus(
    Events.CIRCUIT_STATE_CHANGED,
    (payload) => {
      const p = payload as {
        store?: string;
        game_id?: string;
        is_open?: boolean;
        failure_count?: number;
      };
      if (!gameKey) return;
      // Emitter sends store + game_id separately, not a composed key.
      if (`${p.store}:${p.game_id}` !== gameKey) return;
      setState((prev) => ({
        ...prev,
        open: p.is_open === true,
        failureCount: p.failure_count ?? prev.failureCount,
      }));
    },
    [gameKey],
  );

  const reset = useCallback(async () => {
    if (!gameKey) return;
    try {
      await call<[string], unknown>(rpcRoutes.clearLaunchFailures, gameKey);
    } finally {
      await refetch();
    }
  }, [gameKey, refetch]);

  const forceLaunch = useCallback(async () => {
    if (!gameKey) return;
    try {
      await call<[string], unknown>(rpcRoutes.armCircuitBypass, gameKey);
    } finally {
      await refetch();
    }
  }, [gameKey, refetch]);

  return { ...state, reset, forceLaunch };
}
