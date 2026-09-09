/**
 * Tests for the plugin self-update helpers.
 *
 * These were unreachable by tests until the updater was split out of
 * `components/settings/PluginUpdater.tsx` — which matters, because
 * `resolveInstallAction` is the single source of truth for whether an
 * install is an update, a downgrade or a reinstall, and its prerelease
 * branch exists specifically to stop every dev build being reported as a
 * "downgrade" (a dev tag parses to version 0 under `compareVersions`).
 */
import { describe, it, expect, vi } from "vitest";

// `logEvent` reaches for @decky/api's `call`; nothing under test uses it.
vi.mock("@decky/api", () => ({ call: vi.fn() }));

import {
  INSTALL_TYPE_DOWNGRADE,
  INSTALL_TYPE_REINSTALL,
  INSTALL_TYPE_UPDATE,
  compareVersions,
  extractDevBuildId,
  getPersistedSelectedTag,
  resolveInstallAction,
  setPersistedSelectedTag,
  type ReleaseInfo,
} from "./plugin-update";

const release = (over: Partial<ReleaseInfo> = {}): ReleaseInfo => ({
  tag: "Release-0.7.4",
  version: "0.7.4",
  prerelease: false,
  asset_url: "https://example.invalid/unifideck.prod.v0.7.4.zip",
  asset_name: "unifideck.prod.v0.7.4.zip",
  sha256: "",
  body: "",
  ...over,
});

describe("compareVersions", () => {
  it("orders by numeric component, not lexically", () => {
    expect(compareVersions("0.7.10", "0.7.9")).toBe(1);
    expect(compareVersions("0.7.9", "0.7.10")).toBe(-1);
    expect(compareVersions("0.7.4", "0.7.4")).toBe(0);
  });

  it("treats a missing trailing component as zero", () => {
    expect(compareVersions("0.7", "0.7.0")).toBe(0);
    expect(compareVersions("1", "0.9.9")).toBe(1);
  });

  it("parses a non-numeric component as zero rather than NaN", () => {
    // A dev tag lands here; it must not compare as greater than a release.
    expect(compareVersions("Dev-20260808-171205-47e6d28", "0.0.1")).toBe(-1);
  });
});

describe("extractDevBuildId", () => {
  it("reads the current dev asset naming convention", () => {
    expect(extractDevBuildId("unifideck.dev.0.7.1.g3f9a1c2.zip")).toBe("0.7.1.g3f9a1c2");
  });

  it("still reads the legacy build-number form", () => {
    expect(extractDevBuildId("unifideck.dev.v524.zip")).toBe("v524");
  });

  it("returns null for a production asset or a missing name", () => {
    expect(extractDevBuildId("unifideck.prod.v0.7.4.zip")).toBeNull();
    expect(extractDevBuildId(undefined)).toBeNull();
    expect(extractDevBuildId("")).toBeNull();
  });
});

describe("resolveInstallAction — stable releases", () => {
  it("calls an equal version a reinstall", () => {
    const { installType, displayVersion } = resolveInstallAction(
      release({ version: "0.7.4" }),
      "0.7.4",
      null,
    );
    expect(installType).toBe(INSTALL_TYPE_REINSTALL);
    expect(displayVersion).toBe("0.7.4");
  });

  it("calls a newer version an update", () => {
    expect(resolveInstallAction(release({ version: "0.7.5" }), "0.7.4", null).installType).toBe(
      INSTALL_TYPE_UPDATE,
    );
  });

  it("calls an older version a downgrade", () => {
    expect(resolveInstallAction(release({ version: "0.7.3" }), "0.7.4", null).installType).toBe(
      INSTALL_TYPE_DOWNGRADE,
    );
  });
});

describe("resolveInstallAction — prereleases", () => {
  const dev = release({
    tag: "Dev-20260808-171205-47e6d28",
    version: "Dev-20260808-171205-47e6d28",
    prerelease: true,
    asset_name: "unifideck.dev.0.7.1.g3f9a1c2.zip",
  });

  it("is a reinstall only when the build id matches what is running", () => {
    const { installType, displayVersion } = resolveInstallAction(dev, "0.7.1", "0.7.1.g3f9a1c2");
    expect(installType).toBe(INSTALL_TYPE_REINSTALL);
    // The build id, not the raw non-semver tag, is what the user sees.
    expect(displayVersion).toBe("0.7.1.g3f9a1c2");
  });

  it("is an update for a different build id — never a downgrade", () => {
    expect(resolveInstallAction(dev, "0.7.1", "0.7.1.gdeadbee").installType).toBe(
      INSTALL_TYPE_UPDATE,
    );
  });

  it("is an update on a production install, despite the tag parsing to 0", () => {
    // This is the regression the prerelease branch exists for: routing a
    // dev tag through compareVersions would make every dev install look
    // like a downgrade from the installed release.
    expect(resolveInstallAction(dev, "0.7.4", null).installType).toBe(INSTALL_TYPE_UPDATE);
  });

  it("falls back to the raw version when the asset name is unparseable", () => {
    const odd = { ...dev, asset_name: "manual-upload.zip" };
    expect(resolveInstallAction(odd, "0.7.4", null).displayVersion).toBe(
      "Dev-20260808-171205-47e6d28",
    );
  });
});

describe("persisted release selection", () => {
  it("round-trips a tag and can be cleared", () => {
    setPersistedSelectedTag("Release-0.7.3");
    expect(getPersistedSelectedTag()).toBe("Release-0.7.3");
    setPersistedSelectedTag(null);
    expect(getPersistedSelectedTag()).toBeNull();
  });
});
