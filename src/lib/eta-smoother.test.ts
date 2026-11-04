/**
 * The download ETA swung by many minutes between progress events because
 * each store CLI derives it from its instantaneous speed. These pin the
 * smoothed, held ETA that replaced it.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { ETA_REFRESH_MS, resetEtaSmoother, steadyEta } from "./eta-smoother";

const MIB = 1024 * 1024;
const TOTAL = 10_000 * MIB;

type Input = Parameters<typeof steadyEta>[0];

function dl(over: Partial<Input> = {}): Input {
  return {
    id: "a",
    status: "running",
    download_phase: "downloading",
    downloaded_bytes: 0,
    total_bytes: TOTAL,
    speed_mbps: 40,
    eta_seconds: 250,
    ...over,
  };
}

/** Feed one event per second at the given MiB/s speeds; returns the last ETA. */
function feed(speeds: number[], start: { t: number; bytes: number }, id = "a"): number {
  let eta = 0;
  for (const mibps of speeds) {
    start.t += 1000;
    start.bytes += mibps * MIB;
    eta = steadyEta(
      dl({ id, downloaded_bytes: start.bytes, speed_mbps: mibps, eta_seconds: 1 }),
      start.t,
    );
  }
  return eta;
}

beforeEach(() => resetEtaSmoother());

describe("steadyEta", () => {
  it("shows the backend ETA immediately on the first event", () => {
    expect(steadyEta(dl({ eta_seconds: 321 }), 0)).toBe(321);
  });

  it("holds the shown value until the refresh interval passes", () => {
    steadyEta(dl({ eta_seconds: 250 }), 0);
    const pos = { t: 0, bytes: 0 };
    // 4 s of wildly different backend ETAs: still the first value.
    expect(feed([5, 80, 5, 80], pos)).toBe(250);
  });

  it("refreshes at the interval from the averaged speed", () => {
    steadyEta(dl(), 0);
    const pos = { t: 0, bytes: 0 };
    const eta = feed([40, 40, 40, 40, 40], pos);
    expect(pos.t).toBe(ETA_REFRESH_MS);
    expect(eta).toBe(Math.round((TOTAL - pos.bytes) / (40 * MIB)));
  });

  it("averages out noisy speeds instead of following them", () => {
    steadyEta(dl(), 0);
    const pos = { t: 0, bytes: 0 };
    const etas: number[] = [];
    for (let i = 0; i < 12; i++) {
      etas.push(feed([20, 60, 20, 60, 20], pos));
    }
    const ideal = (TOTAL - pos.bytes) / (36 * MIB); // 180 MiB per 5 s
    const last = etas[etas.length - 1];
    expect(Math.abs(last - ideal) / ideal).toBeLessThan(0.15);
    // Consecutive refreshes never jump like 20 vs 60 MiB/s would (3x).
    for (let i = 1; i < etas.length; i++) {
      expect(etas[i] / etas[i - 1]).toBeLessThan(1.3);
      expect(etas[i] / etas[i - 1]).toBeGreaterThan(0.7);
    }
  });

  it("follows a lasting speed drop within a few time constants", () => {
    steadyEta(dl(), 0);
    const pos = { t: 0, bytes: 0 };
    feed(Array(30).fill(40), pos);
    // After 3 time constants (45 s) ~5% of the 30 MiB/s gap remains:
    // speed reads ~11.5 instead of 10, i.e. the ETA is ~13% short.
    const eta = feed(Array(45).fill(10), pos);
    const ideal = (TOTAL - pos.bytes) / (10 * MIB);
    expect(Math.abs(eta - ideal) / ideal).toBeLessThan(0.15);
  });

  it("starts over when the phase changes or the byte counter goes back", () => {
    steadyEta(dl(), 0);
    const pos = { t: 0, bytes: 0 };
    feed([40, 40, 40], pos);
    expect(
      steadyEta(dl({ download_phase: "extracting", downloaded_bytes: 0, eta_seconds: 77 }), 3500),
    ).toBe(77);
    resetEtaSmoother();
    steadyEta(dl({ downloaded_bytes: 500 * MIB }), 0);
    expect(steadyEta(dl({ downloaded_bytes: 10 * MIB, eta_seconds: 88 }), 1000)).toBe(88);
  });

  it("drops state when it stops running so a restart shows a fresh value", () => {
    steadyEta(dl({ eta_seconds: 250 }), 0);
    expect(steadyEta(dl({ status: "queued", eta_seconds: 0 }), 1000)).toBe(0);
    expect(steadyEta(dl({ eta_seconds: 99 }), 2000)).toBe(99);
  });

  it("falls back to the backend ETA when the total is unknown", () => {
    steadyEta(dl({ total_bytes: 0 }), 0);
    expect(
      steadyEta(
        dl({ total_bytes: 0, downloaded_bytes: 200 * MIB, eta_seconds: 42 }),
        ETA_REFRESH_MS,
      ),
    ).toBe(42);
  });

  it("tracks each download id separately", () => {
    steadyEta(dl({ id: "a", eta_seconds: 100 }), 0);
    steadyEta(dl({ id: "b", eta_seconds: 900 }), 0);
    expect(steadyEta(dl({ id: "a", eta_seconds: 1 }), 1000)).toBe(100);
    expect(steadyEta(dl({ id: "b", eta_seconds: 1 }), 1000)).toBe(900);
  });
});
