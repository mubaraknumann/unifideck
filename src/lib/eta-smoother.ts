/**
 * A download ETA that is steady to read and still tracks real speed changes.
 *
 * Each store CLI reports an ETA from its own instantaneous speed, so the
 * number swung by many minutes between progress events. Here the speed is
 * an exponential moving average of the byte counter, weighted by elapsed
 * time so stores that report at different rates smooth the same way. The
 * shown ETA is recomputed from that average at most once per
 * {@link ETA_REFRESH_MS}; between refreshes the last value is held.
 *
 * State is keyed by download id, so every surface rendering the same
 * download shows the same ETA.
 */
import type { DownloadItem } from "../types/downloads";

/** How long a shown ETA is held before it is recomputed. */
export const ETA_REFRESH_MS = 5000;
/** Time constant of the speed average: a lasting speed change is mostly
 *  absorbed after about this long. */
export const SPEED_TAU_MS = 15000;
/** Samples closer together than this are skipped (the same event rendered
 *  by two components, or a burst of events with no new bytes). */
const MIN_SAMPLE_MS = 250;
/** `speed_mbps` is MiB/s (backend divides bytes/s by 1024²). */
const BYTES_PER_MIB = 1024 * 1024;

interface EtaState {
  emaBps: number;
  lastBytes: number;
  lastAt: number;
  phase: string | undefined;
  shownEta: number;
  shownAt: number;
}

const states = new Map<string, EtaState>();

type EtaInput = Pick<
  DownloadItem,
  | "id"
  | "status"
  | "download_phase"
  | "downloaded_bytes"
  | "total_bytes"
  | "speed_mbps"
  | "eta_seconds"
>;

function freshState(d: EtaInput, now: number): EtaState {
  return {
    emaBps: Math.max(0, d.speed_mbps) * BYTES_PER_MIB,
    lastBytes: d.downloaded_bytes,
    lastAt: now,
    phase: d.download_phase,
    shownEta: d.eta_seconds,
    shownAt: now,
  };
}

function sample(s: EtaState, bytes: number, now: number): void {
  const dt = now - s.lastAt;
  if (dt < MIN_SAMPLE_MS || bytes <= s.lastBytes) return;
  const instBps = ((bytes - s.lastBytes) * 1000) / dt;
  const alpha = 1 - Math.exp(-dt / SPEED_TAU_MS);
  s.emaBps += alpha * (instBps - s.emaBps);
  s.lastBytes = bytes;
  s.lastAt = now;
}

function estimate(s: EtaState, d: EtaInput): number {
  if (d.total_bytes > 0 && s.emaBps > 0) {
    const remaining = Math.max(0, d.total_bytes - d.downloaded_bytes);
    return Math.round(remaining / s.emaBps);
  }
  return d.eta_seconds;
}

/** ETA in seconds to display for `d` at `now`. */
export function steadyEta(d: EtaInput, now: number = Date.now()): number {
  if (d.status !== "running") {
    states.delete(d.id);
    return d.eta_seconds;
  }
  const prev = states.get(d.id);
  if (
    !prev ||
    prev.phase !== d.download_phase ||
    d.downloaded_bytes < prev.lastBytes
  ) {
    const s = freshState(d, now);
    states.set(d.id, s);
    return s.shownEta;
  }
  sample(prev, d.downloaded_bytes, now);
  if (now - prev.shownAt >= ETA_REFRESH_MS) {
    prev.shownEta = estimate(prev, d);
    prev.shownAt = now;
  }
  return prev.shownEta;
}

/** Forget every tracked download (tests). */
export function resetEtaSmoother(): void {
  states.clear();
}
