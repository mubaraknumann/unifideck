"""launcher/xcloud_player — Unifideck's own xCloud player (no xbox.com).

A stream runs in a kiosk browser window on a local page built from
Greenlight's MIT player library (``web/``, sources in ``xcloud-player/``).
The launcher holds every token and makes every xCloud call
(``credentials``, ``api``, ``server``); the page only does WebRTC and input.

Measured on a Steam Deck 2026-11-03 (POC): streaming token from the saved
OAuth sign-in, session ``Provisioned`` after ``/connect`` with the
console-transfer ticket, 1920x1080 at ~60 fps with audio in Chromium.
"""
