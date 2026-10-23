import { describe, expect, it } from "vitest";
import {
  disambiguationSuffixes,
  labelCandidates,
  readDisambiguators,
  type LabelCandidate,
} from "./variant-labels";

const candidate = (id: number, entries: string[], base = "gamevault\u0000"): LabelCandidate => ({
  id,
  base,
  entries,
});

describe("readDisambiguators", () => {
  it("trims entries and drops empty and non-string ones", () => {
    expect(readDisambiguators({ disambiguators: [" Linux ", "", 3, "Ghostship"] })).toEqual([
      "Linux",
      "Ghostship",
    ]);
  });

  it("returns undefined for anything without a usable string", () => {
    expect(readDisambiguators(undefined)).toBeUndefined();
    expect(readDisambiguators({})).toBeUndefined();
    expect(readDisambiguators({ disambiguators: "Linux" })).toBeUndefined();
    expect(readDisambiguators({ disambiguators: ["", 1, null] })).toBeUndefined();
  });
});

describe("disambiguationSuffixes", () => {
  it("advances the whole group until every label is distinct", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux", "Ghostship"]),
      candidate(2, ["Linux", "Render96ex"]),
      candidate(3, ["Linux", "sm64coopdx"]),
      candidate(4, ["Windows", "Render96ex"]),
    ]);
    expect(suffixes.get(1)).toBe("Linux · Ghostship");
    expect(suffixes.get(2)).toBe("Linux · Render96ex");
    expect(suffixes.get(3)).toBe("Linux · sm64coopdx");
    expect(suffixes.get(4)).toBe("Windows · Render96ex");
  });

  it("stops at depth one when one entry separates the group", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux", "x"]),
      candidate(2, ["Windows", "x"]),
    ]);
    expect(suffixes.get(1)).toBe("Linux");
    expect(suffixes.get(2)).toBe("Windows");
  });

  it("adds nothing when the base labels already differ", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux"], "epic\u0000"),
      candidate(2, ["Linux"], "gog\u0000"),
    ]);
    expect(suffixes.get(1)).toBe("");
    expect(suffixes.get(2)).toBe("");
  });

  it("renders what a short list has and nothing for a member without entries", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux", "A"]),
      candidate(2, ["Linux"]),
      candidate(3, []),
    ]);
    expect(suffixes.get(1)).toBe("Linux · A");
    expect(suffixes.get(2)).toBe("Linux");
    expect(suffixes.get(3)).toBe("");
  });

  it("goes as deep as the entries need, however many there are", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux", "A", "v1.0"]),
      candidate(2, ["Linux", "A", "v2.0"]),
    ]);
    expect(suffixes.get(1)).toBe("Linux · A · v1.0");
    expect(suffixes.get(2)).toBe("Linux · A · v2.0");
  });

  it("keeps identical labels identical when no depth separates the group", () => {
    const suffixes = disambiguationSuffixes([
      candidate(1, ["Linux", "A"]),
      candidate(2, ["Linux", "A"]),
    ]);
    expect(suffixes.get(1)).toBe("Linux · A");
    expect(suffixes.get(2)).toBe("Linux · A");
  });

  it("returns an empty suffix for a single candidate", () => {
    expect(disambiguationSuffixes([candidate(1, ["Linux"])]).get(1)).toBe("");
  });
});

describe("labelCandidates", () => {
  const sibling = (appId: number, editionLabel: string | undefined, disambiguators: string[]) => ({
    appId,
    store: "gamevault",
    editionLabel,
    disambiguators,
  });

  it("treats a missing edition and an explicit placeholder-equal one as the same label", () => {
    const suffixes = disambiguationSuffixes(
      labelCandidates(
        [sibling(1, undefined, ["Linux"]), sibling(2, "Standard Edition", ["Windows"])],
        "Standard Edition",
      ),
    );
    expect(suffixes.get(1)).toBe("Linux");
    expect(suffixes.get(2)).toBe("Windows");
  });

  it("keeps genuinely different editions apart without a suffix", () => {
    const suffixes = disambiguationSuffixes(
      labelCandidates(
        [sibling(1, "Deluxe", ["Linux"]), sibling(2, undefined, ["Linux"])],
        "Standard Edition",
      ),
    );
    expect(suffixes.get(1)).toBe("");
    expect(suffixes.get(2)).toBe("");
  });
});
