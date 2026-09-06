/**
 * Render Valve's compatibility test-result tokens.
 *
 * The backend stores each result as Valve's own `loc_token`
 * (`#SteamDeckVerified_TestResult_*`, `#SteamMachine_TestResult_*`,
 * `#SteamOS_TestResult_*`) rather than English prose, and we resolve it
 * here through the Steam client's own localisation manager.
 *
 * Why not a table of our own: the installed client ships 37 Deck, 27
 * Machine and 27 SteamOS strings, already translated into the user's
 * Steam language. Our previous hand-written table had 9 of the 37, in
 * English only — so a Japanese Deck showed English, and 28 real
 * verification reasons were silently dropped. Maintaining our own copy
 * would mean ~91 strings to write and 16 locales to translate them
 * into, going stale the next time Valve ships hardware.
 *
 * `LocalizationManager` is undocumented and version-dependent, so it is
 * feature-detected once and every call is guarded. `FALLBACK_EN` covers
 * a Steam build too old to know a token — precisely the build where our
 * own translation would be guesswork too, hence English-only and no
 * i18n keys.
 */

/** One test result as the backend stores it. */
export interface CompatTestResult {
  /** Valve's loc token. Absent on entries cached before the rework. */
  token?: string;
  /** Pre-resolved English, from those older entries. */
  text?: string;
  passed: boolean;
}

interface LocalizationManagerLike {
  LocalizeIfToken?: (token: string) => string | undefined;
  LocalizeString?: (token: string) => string | undefined;
}

/**
 * Last-resort English for tokens the running Steam build cannot
 * resolve. Deliberately small — the client is the source of truth.
 */
const FALLBACK_EN: Record<string, string> = {
  "#SteamDeckVerified_TestResult_DefaultControllerConfigFullyFunctional":
    "All functionality is accessible when using the default controller configuration",
  "#SteamDeckVerified_TestResult_ControllerGlyphsMatchDeckDevice":
    "This game shows Steam Deck controller icons",
  "#SteamDeckVerified_TestResult_InterfaceTextIsLegible":
    "In-game interface text is legible on Steam Deck",
  "#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant":
    "This game's default graphics configuration performs well on Steam Deck",
  "#SteamMachine_TestResult_DefaultControllerConfigFullyFunctional":
    "All functionality is accessible when using the default controller configuration",
  "#SteamMachine_TestResult_ControllerGlyphsMatchDevice":
    "This game shows Steam controller icons",
  "#SteamMachine_TestResult_DefaultConfigurationIsPerformant":
    "This game's default graphics configuration performs well on Steam Machine",
  "#SteamOS_TestResult_GameStartupFunctional":
    "This game runs successfully on SteamOS",
};

function manager(): LocalizationManagerLike | null {
  try {
    const lm = (window as unknown as { LocalizationManager?: unknown })
      .LocalizationManager;
    return lm && typeof lm === "object"
      ? (lm as LocalizationManagerLike)
      : null;
  } catch {
    return null;
  }
}

/** Resolve one token through Steam's localisation, or null. */
function localizeToken(token: string): string | null {
  const lm = manager();
  if (!lm) return null;
  try {
    const resolved = lm.LocalizeIfToken?.(token) ?? lm.LocalizeString?.(token);
    // A token Steam does not know comes back as the token itself (or
    // empty) — treat that as unresolved so the fallback gets a turn.
    if (typeof resolved !== "string") return null;
    const trimmed = resolved.trim();
    if (!trimmed || trimmed === token) return null;
    return trimmed;
  } catch {
    return null;
  }
}

/**
 * Display text for one test result, or null to drop the row.
 *
 * Dropping is deliberate: an unresolvable token has nothing to show,
 * and inventing prose for it is how a stale English table starts.
 */
export function renderTestResult(result: CompatTestResult): string | null {
  if (result.token) {
    return localizeToken(result.token) ?? FALLBACK_EN[result.token] ?? null;
  }
  // Entries cached before the rework carry resolved English instead.
  const text = result.text?.trim();
  return text || null;
}

/** Test-result rows that have something to display, in order. */
export function renderTestResults(
  results: readonly CompatTestResult[] | undefined,
): Array<{ text: string; passed: boolean }> {
  if (!Array.isArray(results)) return [];
  const out: Array<{ text: string; passed: boolean }> = [];
  for (const result of results) {
    const text = renderTestResult(result);
    if (text) out.push({ text, passed: !!result.passed });
  }
  return out;
}
