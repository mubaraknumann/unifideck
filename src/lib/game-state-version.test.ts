import { describe, expect, it } from "vitest";
import { bumpGameStateVersion, getGameStateVersion } from "./game-state-version";

// The backend sends a shortcut's appid signed; the app-details page reads it
// unsigned. Both forms must address the same counter.
const SIGNED = -584483551;
const UNSIGNED = 3710483745;

describe("game-state-version", () => {
  it("a bump under the signed appid is read under the unsigned one", () => {
    const before = getGameStateVersion(UNSIGNED);
    bumpGameStateVersion(SIGNED);
    expect(getGameStateVersion(UNSIGNED)).toBe(before + 1);
    expect(getGameStateVersion(SIGNED)).toBe(before + 1);
  });

  it("a bump under the unsigned appid is read under the signed one", () => {
    const before = getGameStateVersion(SIGNED);
    bumpGameStateVersion(UNSIGNED);
    expect(getGameStateVersion(SIGNED)).toBe(before + 1);
  });

  it("leaves real Steam appids as they are", () => {
    bumpGameStateVersion(257350);
    expect(getGameStateVersion(257350)).toBeGreaterThan(0);
    expect(getGameStateVersion(257351)).toBe(0);
  });
});
