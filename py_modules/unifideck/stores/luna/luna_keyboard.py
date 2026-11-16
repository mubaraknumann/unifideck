"""Luna virtual keyboard: the reused CDP page-inject on-screen keyboard.

Steam's overlay keyboard is unavailable when the Luna kiosk browser runs
outside Steam, so an on-screen keyboard is injected **into the page over
CDP** (Req 7). This module does not invent a new injection mechanism: it
reuses the shared ``unifideck.cdp.page_inject.inject_scripts`` seam — the
same CDP page-inject the stream path already uses — and injects over the
**same browser-game CDP port** the spoofer and play-activator attached to
(``browser_game_cdp_port()``), so there is one CDP session for the whole
Luna launch.

The split mirrors the rest of the Luna package: the keyboard JS is built by
a **pure, I/O-free** function (:func:`build_virtual_keyboard_script`) that
is unit-testable without a browser, while :func:`inject_virtual_keyboard`
is the thin impure wrapper that hands the script to ``inject_scripts``.

Layout selection (Req 7.3, 7.4): AZERTY for any locale whose language code
is ``fr``, QWERTY for everything else, and QWERTY as the default when the
locale cannot be resolved. The injected script wires the show/hide to the
page's own ``focusin``/``focusout`` events so the keyboard appears within
500 ms of a text field gaining focus and hides on blur (Req 7.1, 7.2); that
timing is a property of the page's event loop once the one-shot script is
installed, so the injection itself only has to land once per session.
"""
from __future__ import annotations

import json
import logging

from unifideck.cdp.page_inject import inject_scripts

logger = logging.getLogger(__name__)

#: QWERTY rows — the default layout (Req 7.4) and every non-``fr`` locale.
_QWERTY_ROWS = [
    list("1234567890"),
    list("qwertyuiop"),
    list("asdfghjkl"),
    list("zxcvbnm"),
]

#: AZERTY rows — selected only for a ``fr`` language code (Req 7.3).
_AZERTY_ROWS = [
    list("1234567890"),
    list("azertyuiop"),
    list("qsdfghjklm"),
    list("wxcvbn"),
]

#: The CDP log tag so a keyboard inject is attributable in the launcher log.
_KEYBOARD_LOG_PREFIX = "luna.keyboard"

#: Sentinel tokens substituted at build time. Using ``str.replace`` on unique
#: sentinels (rather than ``str.format``/f-string interpolation) keeps the
#: embedded JS braces untouched, so the emitted script stays byte-for-byte
#: identical to the historical f-string output.
_ROWS_SENTINEL = "__ROWS_LITERAL__"
_LAYOUT_SENTINEL = "__LAYOUT_JSON__"

#: The one-shot keyboard-install JS, hoisted out of
#: :func:`build_virtual_keyboard_script` so that function stays under the
#: 80-line volumetry cap. ``{ROWS}`` / ``{LAYOUT}`` sentinels are replaced
#: with ``json.dumps(rows)`` and ``json.dumps(layout)`` respectively. The
#: body is byte-for-byte identical to the previous inline template.
_KEYBOARD_SCRIPT_TEMPLATE = """
(() => {
  if (window.__unifideckLunaKeyboard) { return 'already-installed'; }
  const ROWS = __ROWS_LITERAL__;
  const LAYOUT = __LAYOUT_JSON__;
  const SHOW_HIDE_MS = 500;
  let activeField = null;
  let hideTimer = null;

  const kb = document.createElement('div');
  kb.id = 'unifideck-luna-keyboard';
  kb.style.cssText = [
    'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
    'display:none', 'flex-direction:column', 'gap:6px', 'padding:10px',
    'background:rgba(20,20,24,0.96)', 'font-family:sans-serif',
  ].join(';');

  const typeInto = (text) => {
    if (!activeField) { return; }
    const start = activeField.selectionStart;
    if (typeof start === 'number' && 'setRangeText' in activeField) {
      activeField.setRangeText(text, start, activeField.selectionEnd, 'end');
    } else {
      activeField.value = (activeField.value || '') + text;
    }
    activeField.dispatchEvent(new Event('input', { bubbles: true }));
  };

  for (const row of ROWS) {
    const rowEl = document.createElement('div');
    rowEl.style.cssText = 'display:flex;gap:6px;justify-content:center';
    for (const key of row) {
      const btn = document.createElement('button');
      btn.textContent = key;
      btn.type = 'button';
      btn.style.cssText =
        'min-width:42px;min-height:42px;font-size:18px;border-radius:6px';
      // Keep focus on the field so focusout does not hide the keyboard.
      btn.addEventListener('mousedown', (e) => e.preventDefault());
      btn.addEventListener('click', () => typeInto(key));
      rowEl.appendChild(btn);
    }
    kb.appendChild(rowEl);
  }

  const show = () => {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    kb.style.display = 'flex';
  };
  const hideSoon = () => {
    hideTimer = setTimeout(() => { kb.style.display = 'none'; }, SHOW_HIDE_MS);
  };

  const isTextField = (el) =>
    !!el && (el.tagName === 'TEXTAREA' ||
      (el.tagName === 'INPUT' && !['button','submit','checkbox','radio','range']
        .includes((el.type || 'text').toLowerCase())) ||
      el.isContentEditable);

  document.addEventListener('focusin', (e) => {
    if (isTextField(e.target)) { activeField = e.target; show(); }
  }, true);
  document.addEventListener('focusout', (e) => {
    if (e.target === activeField) { activeField = null; hideSoon(); }
  }, true);

  const mount = () => {
    if (document.body) { document.body.appendChild(kb); }
  };
  if (document.body) { mount(); }
  else { document.addEventListener('DOMContentLoaded', mount); }

  window.__unifideckLunaKeyboard = { layout: LAYOUT };
  return 'installed:' + LAYOUT;
})()
"""


def select_layout(locale: str | None) -> str:
    """Choose the keyboard layout name from the Unifideck locale (Req 7.3, 7.4).

    AZERTY for any locale whose language code (the part before the first
    ``-``) is ``fr``; QWERTY for every other locale and as the default when
    the locale is unavailable or cannot be resolved. Pure.
    """
    if not locale:
        return "qwerty"
    language = locale.split("-", 1)[0].strip().lower()
    return "azerty" if language == "fr" else "qwerty"


def _rows_for_layout(layout: str) -> list[list[str]]:
    """The key rows for a resolved layout name (defaults to QWERTY)."""
    return _AZERTY_ROWS if layout == "azerty" else _QWERTY_ROWS


def build_virtual_keyboard_script(locale: str | None) -> str:
    """Build the one-shot JS that installs the Luna virtual keyboard (Req 7).

    PURE: no I/O, so the layout selection and the embedded key set are
    unit-testable without a browser. The returned script, once evaluated in
    the Luna page, installs an on-screen keyboard that:

    - selects its layout from ``locale`` (AZERTY for ``fr``, else QWERTY —
      Req 7.3/7.4), baked in at build time via :func:`select_layout`;
    - shows itself within 500 ms of a text field gaining focus
      (``focusin``) and hides within 500 ms of blur (``focusout``) —
      Req 7.1/7.2 — by toggling its own element's visibility on those
      page events;
    - types into the focused field by dispatching ``input`` events, so the
      streamed Luna page sees ordinary text entry.

    The script is idempotent: it no-ops if a Luna keyboard element already
    exists, so a re-inject over the same session never stacks keyboards.
    """
    layout = select_layout(locale)
    rows = _rows_for_layout(layout)
    rows_literal = json.dumps(rows)
    return (
        _KEYBOARD_SCRIPT_TEMPLATE
        .replace(_ROWS_SENTINEL, rows_literal)
        .replace(_LAYOUT_SENTINEL, json.dumps(layout))
        .strip()
    )


async def inject_virtual_keyboard(
    port: int,
    locale: str | None,
    *,
    url_patterns: list[str],
    timeout: float = 15.0,  # noqa: ASYNC109 — passed through to inject_scripts/aiohttp
) -> bool:
    """Inject the Luna virtual keyboard over the browser-game CDP port (Req 7).

    Thin wrapper over the reused :func:`unifideck.cdp.page_inject.inject_scripts`
    seam — the same CDP page-inject the stream path uses — targeting the
    Luna page on the **same** ``port`` (``browser_game_cdp_port()``) the
    spoofer and play-activator attached to, so the whole launch runs over one
    CDP session.

    The caller (the Luna launch flow) is responsible for aborting the launch
    and logging the missing-CDP condition when CDP is unavailable (Req 7.5);
    this function returns ``False`` when no matching Luna page could be
    injected into so the caller can decide.
    """
    script = build_virtual_keyboard_script(locale)
    logger.info(
        "[Luna] injecting virtual keyboard (%s) on CDP port %d",
        select_layout(locale), port,
    )
    return await inject_scripts(
        port,
        [script],
        url_patterns=url_patterns,
        timeout=timeout,
        logger_prefix=_KEYBOARD_LOG_PREFIX,
    )
