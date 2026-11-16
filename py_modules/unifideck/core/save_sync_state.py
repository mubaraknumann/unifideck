"""How a game's local saves compare with its cloud copy — one vocabulary for every store.

Two systems answer this question:

* Epic, GOG and EA, which Unifideck syncs itself (``services/cloud_save``):
  a three-way compare against the last recorded sync (``sync_record.py``), or
  the store's own file-by-file compare (EA).
* Xbox PC, which the Xodus runtime syncs inside the game
  (``stores/microsoft/xodus/save_status.py``): the same three-way compare,
  against the runtime's own index.

Both report these words, and the frontend maps them to one status line
(``src/lib/cloud-save-line.ts``). Before this module the Xbox side called the
same states ``waiting_upload`` and ``both_changed``.

Pure data, no imports: ``core/`` is the one layer both sides can reach.
"""
from __future__ import annotations

#: Both copies hold the same saves.
IN_SYNC = "in_sync"
#: This device has saves the cloud does not: an upload is due.
LOCAL_NEWER = "local_newer"
#: The cloud has saves this device does not: a download is due.
CLOUD_NEWER = "cloud_newer"
#: Both changed since the last sync; one side must be chosen.
CONFLICT = "conflict"

VERDICTS = frozenset({IN_SYNC, LOCAL_NEWER, CLOUD_NEWER, CONFLICT})
