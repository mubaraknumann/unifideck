import { describe, it, expect, vi } from "vitest";

vi.mock("@decky/ui", () => ({
  ConfirmModal: () => null,
  DialogButton: () => null,
  Field: () => null,
  TextField: () => null,
  ToggleField: () => null,
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));
vi.mock("./StoragePathPicker", () => ({ StoragePathPicker: () => null }));

import { pairingQr, choosableVariants, type RomMVariant } from "./RomMConnectModal";

const v = (platform_slug: string, fs_slug: string): RomMVariant => ({
  platform_slug,
  fs_slug,
  display_name: fs_slug,
});

describe("pairingQr", () => {
  it("encodes locally with a four-module quiet zone", () => {
    const qr = pairingQr("https://romm.example/pair/device?user_code=ABCD1234");
    expect(qr).not.toBeNull();
    const points = [...qr!.path.matchAll(/M(\d+),(\d+)/g)].map((m) => [Number(m[1]), Number(m[2])]);
    expect(points.length).toBeGreaterThan(100);
    expect(points.every(([x, y]) => x >= 4 && y >= 4 && x < qr!.size - 4 && y < qr!.size - 4)).toBe(
      true,
    );
    expect(pairingQr("https://romm.example/pair/device?user_code=OTHER")!.path).not.toBe(qr!.path);
  });

  it("leaves text fallback available when the address exceeds QR capacity", () => {
    expect(pairingQr("x".repeat(10000))).toBeNull();
  });
});

describe("choosableVariants", () => {
  it("offers no choice when each canonical platform has one folder", () => {
    expect(choosableVariants([v("linux", "linux"), v("win", "windows")])).toEqual([]);
  });

  it("offers every folder of a platform hosted under several", () => {
    const all = [v("linux", "linux"), v("win", "windows"), v("win", "windows9x")];
    expect(choosableVariants(all).map((x) => x.fs_slug)).toEqual(["windows", "windows9x"]);
  });

  it("offers nothing for an empty server", () => {
    expect(choosableVariants([])).toEqual([]);
  });
});
