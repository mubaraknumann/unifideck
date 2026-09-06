"""Language matching utility — re-export shim.

Both functions now live in ``unifideck.utils.lang_normalize``:

* ``normalize_language(raw)`` → ISO 639-1 base code, and
* ``smart_match_language(requested, supported)`` → the best of the
  labels a title actually offers.

They were hoisted out of this GOG-scoped module when Epic needed the
same matching for legendary's SDL install tags — a store package must
not import another store's internals. This shim stays so existing
importers (and tests) can keep importing either name from here.

The shared module mirrors the frontend
``src/lib/i18n/gog-language-match.ts``.
"""

from __future__ import annotations

from unifideck.utils.lang_normalize import normalize_language, smart_match_language

__all__ = ["normalize_language", "smart_match_language"]
