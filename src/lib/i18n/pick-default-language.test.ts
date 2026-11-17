/**
 * Tests for the install language picker's pre-selection.
 *
 * The regression that prompted these: Epic's Selective Downloads
 * configs list only the *additional* language packs a title offers —
 * English is the untagged base game and so appears nowhere in the list.
 * Fed that list, `pickDefaultLanguage` fell through "user's language"
 * and "English" to `options[0]` and pre-selected **Deutsch**, so an
 * English user who accepted the dialog downloaded a German voice pack.
 *
 * The backend now always includes a base-language entry for such titles
 * (`py_modules/unifideck/stores/epic/sdl.py`); these tests pin both
 * halves — that a present English choice always beats the positional
 * fallback, and what that fallback does when it has nothing to work with.
 */
import { describe, it, expect } from "vitest";
import { pickDefaultLanguage } from "./pick-default-language";

// Exactly what `get_install_languages` reports (Epic) for Hogwarts Legacy and
// Suicide Squad, in order, with the synthetic base entry first.
const EPIC_SDL_OPTIONS = ["en", "de", "es-ES", "es-MX", "fr", "it", "ja", "pt-BR"];

describe("pickDefaultLanguage", () => {
  it("pre-selects English for an English UI (the UD-026 regression)", () => {
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "en-US")).toBe("en");
    // Never the first non-English option, which is what users saw.
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "en-US")).not.toBe("de");
  });

  it("pre-selects the user's own language when offered", () => {
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "fr-FR")).toBe("fr");
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "de-DE")).toBe("de");
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "ja-JP")).toBe("ja");
  });

  it("keeps regional variants distinct", () => {
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "es-MX")).toBe("es-MX");
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "es-ES")).toBe("es-ES");
  });

  it("falls back to English when the UI language isn't offered", () => {
    // Thai isn't on offer; English is the safe landing, not options[0].
    expect(pickDefaultLanguage(EPIC_SDL_OPTIONS, "th-TH")).toBe("en");
  });

  it("still handles GOG's own label formats", () => {
    const gog = ["English", "French", "Deutsch", "espa\u00f1ol"];
    expect(pickDefaultLanguage(gog, "en-US")).toBe("English");
    expect(pickDefaultLanguage(gog, "de-DE")).toBe("Deutsch");
    expect(pickDefaultLanguage(gog, "th-TH")).toBe("English");
  });

  it("handles EA build locales, sent as region tags", () => {
    // `get_install_languages` (EA): the build's `<locales>`, `_` → `-`.
    const ea = ["en-US", "cs-CZ", "de-DE", "pl-PL", "zh-TW"];
    expect(pickDefaultLanguage(ea, "en")).toBe("en-US");
    expect(pickDefaultLanguage(ea, "de")).toBe("de-DE");
    expect(pickDefaultLanguage(ea, "pl-PL")).toBe("pl-PL");
    expect(pickDefaultLanguage(ea, "th-TH")).toBe("en-US");
  });

  it("uses the first option only when nothing matches at all", () => {
    // No English anywhere and no match — the positional fallback is all
    // that is left. Pinned so it stays a deliberate last resort.
    expect(pickDefaultLanguage(["de", "fr"], "th-TH")).toBe("de");
  });
});
