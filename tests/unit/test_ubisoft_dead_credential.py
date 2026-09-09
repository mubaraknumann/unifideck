"""A Ubisoft credential UPC has rejected must be detectable.

We inject the auth prefix's credential into every game prefix before running
UPC. If UPC then leaves that prefix SIGNED OUT, it never accepted what we gave
it — the stored token is dead server-side (Ubisoft rotates and invalidates).

Nothing used to notice. ``capture`` refuses to capture back FROM a signed-out
prefix, so the dead token stays put and every later install injects it again:
an endless sign-in prompt with no UI affordance to break out of. Confirmed live
2026-08-01 — the Aug-1 04:55 auth credential was injected into a fresh prefix
(``inject: synced 4 credential file(s)``) and UPC demanded a sign-in anyway.

"Signed out" is a question about *shape*, not size: a vault with no ``user.dat``
beside it holds no account. It was a size comparison against the auth prefix
until GH #435 showed that rotated vaults are routinely smaller, which made a
healthy rotation indistinguishable from a sign-out and froze the auth prefix on
a retired token.

The detector is deliberately **report-only**: purging a user's credentials is
their call (QAM → Ubisoft → Sign out), never a heuristic's side effect.
"""
from __future__ import annotations

from types import SimpleNamespace

from unifideck.stores.ubisoft.session.facade import UbisoftSession


def _session(
    signed_in: dict[str, bool],
    has_creds: dict[str, bool] | None = None,
    auth_dir: str = "/auth",
):
    """A session whose reader reports the given state per prefix path."""
    creds = has_creds if has_creds is not None else signed_in
    sess = UbisoftSession.__new__(UbisoftSession)
    sess._config = SimpleNamespace(auth_prefix_dir_expanded=auth_dir)
    sess._reader = SimpleNamespace(
        is_signed_in=lambda p: signed_in.get(p, False),
        has_valid_credentials=lambda p: creds.get(p, False),
    )
    return sess


def test_signed_out_prefix_after_injection_is_reported():
    """The live failure: UPC signed out of a prefix we had signed in."""
    sess = _session({"/auth": True, "/games/80": False})

    assert sess.stored_credential_was_rejected("/games/80") is True


def test_healthy_prefix_is_not_reported():
    """UPC kept the session → nothing to report."""
    sess = _session({"/auth": True, "/games/80": True})

    assert sess.stored_credential_was_rejected("/games/80") is False


def test_no_stored_credential_is_not_a_rejection():
    """Signed out by choice → a sign-in prompt is expected, not a defect."""
    sess = _session({"/auth": False, "/games/80": False})

    assert sess.stored_credential_was_rejected("/games/80") is False


def test_prefix_without_a_credential_is_not_a_rejection():
    """A never-run prefix has no credential to judge.

    It has no ``user.dat`` either, so the signed-in test alone would call this
    a rejection; ``has_valid_credentials`` on the auth prefix is what keeps
    the report about prefixes we actually seeded.
    """
    sess = _session(
        {"/auth": True, "/games/80": False},
        has_creds={"/auth": True, "/games/80": False},
    )

    assert sess.stored_credential_was_rejected("/games/80") is True


def test_a_smaller_credential_is_not_a_rejection_by_itself():
    """The GH #435 regression: a rotated token is often smaller.

    Size carries no verdict any more. A prefix that is still signed in is
    healthy however much its vault shrank.
    """
    sess = _session({"/auth": True, "/games/80": True})

    assert sess.stored_credential_was_rejected("/games/80") is False


def test_detector_does_not_mutate_anything():
    """Report-only: no purge, no write, no clear."""
    calls: list[str] = []
    sess = _session({"/auth": True, "/games/80": False})
    for forbidden in (
        "purge_credentials_from_all", "clear_session_file", "_write_stored_mtime",
    ):
        setattr(sess, forbidden, lambda *a, **k: calls.append(forbidden))

    sess.stored_credential_was_rejected("/games/80")

    assert calls == []
