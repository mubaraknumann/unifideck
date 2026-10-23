/**
 * variant-labels: tells apart store switcher rows that would read the same.
 *
 * A store lists display strings in `metadata.disambiguators`, most telling
 * first. For each group this appends as many of them as it takes for every
 * row to differ. Every row gets the same number of entries, so rows stay
 * comparable. Rows that no entry separates keep identical labels; no marker
 * is invented for them.
 */

export const ENTRY_SEPARATOR = " · ";

export interface LabelCandidate {
  id: number;
  /** What the row already shows (store and edition). */
  base: string;
  entries: readonly string[];
}

/** A store's `disambiguators` from a row's `metadata`: trimmed non-empty
 *  strings only, or `undefined` when none are usable. */
export function readDisambiguators(
  metadata: Record<string, unknown> | undefined,
): string[] | undefined {
  const raw = metadata?.disambiguators;
  if (!Array.isArray(raw)) return undefined;
  const entries = raw
    .filter((e): e is string => typeof e === "string")
    .map((e) => e.trim())
    .filter((e) => e !== "");
  return entries.length > 0 ? entries : undefined;
}

function labelAt(candidate: LabelCandidate, depth: number): string {
  return candidate.entries.slice(0, depth).join(ENTRY_SEPARATOR);
}

/** The text to append to each candidate's label, keyed by id. `""` means
 *  nothing. */
export function disambiguationSuffixes(
  candidates: readonly LabelCandidate[],
): Map<number, string> {
  const longest = Math.max(0, ...candidates.map((c) => c.entries.length));
  let chosen = 0;
  if (candidates.length > 1) {
    chosen = longest;
    for (let depth = 0; depth <= longest; depth++) {
      const labels = candidates.map(
        (c) => `${c.base}\u0001${labelAt(c, depth)}`,
      );
      if (new Set(labels).size === labels.length) {
        chosen = depth;
        break;
      }
    }
  }
  return new Map(candidates.map((c) => [c.id, labelAt(c, chosen)]));
}

/** Candidates for a list of switcher siblings. A member with no edition shows
 *  `placeholder`, so it stands in here too; otherwise two rows that read the
 *  same would count as different. */
export function labelCandidates(
  siblings: readonly {
    appId: number;
    store: string;
    editionLabel?: string;
    disambiguators?: readonly string[];
  }[],
  placeholder: string,
): LabelCandidate[] {
  return siblings.map((s) => ({
    id: s.appId,
    base: `${s.store}\u0000${s.editionLabel ?? placeholder}`,
    entries: s.disambiguators ?? [],
  }));
}
