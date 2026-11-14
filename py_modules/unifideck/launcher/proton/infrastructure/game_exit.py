"""What a umu game run's exit code means, shared by every launch handler.

``rc`` is the game's own exit code: each handler awaits the umu-run it spawned,
so the launcher lives exactly as long as the game and Steam tracks the session.
0 is a normal exit; 2 and 74 are umu bootstrap failures that a retry cannot fix;
anything else is the game failing.
"""

from __future__ import annotations

from typing import Any

from unifideck.launcher.types.errors import GameFailedError, UmuRuntimeError


def finish_game_run(plan: Any, rc: int) -> int:
    """Record ``rc`` on the plan; return 0 or raise the matching launcher error."""
    store = plan.context.store
    plan.state.game_exit_code = rc
    if rc == 0:
        return 0
    if rc in {2, 74}:
        raise UmuRuntimeError(
            f"umu-run failed with unrecoverable code {rc}",
            context={"subprocess_rc": rc, "store": store},
        )
    raise GameFailedError(
        f"{store} game exited with code {rc}",
        subprocess_rc=rc,
        context={"store": store, "game_id": plan.context.game_id},
    )
