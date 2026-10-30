from unifideck.stores.microsoft import microsoft_auth as m


def test_prefixed_ticket_is_not_prefixed_twice(monkeypatch):
    seen = []

    def fake(cv, rps, *a, **k):
        seen.append(rps)
        return {"Token": "x"}

    monkeypatch.setattr(m, "_try_xbl_request", fake)
    m._obtain_xbl_user_token("t=eyJabc", "en-US", "u", "ua")
    assert seen == ["t=eyJabc"]
