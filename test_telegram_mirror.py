import ebay_deal_alert as engine


class _Resp:
    def __init__(self, ok):
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise engine.requests.exceptions.HTTPError("bad")


def test_mirror_off_without_secrets(monkeypatch):
    monkeypatch.setattr(engine, "TELEGRAM_BOT_TOKEN", "")
    assert engine.telegram_mirror("t", "m") is False


def test_photo_failure_falls_back_to_text(monkeypatch):
    calls = []

    def fake_post(url, timeout, data):
        calls.append((url.rsplit("/", 1)[1], data))
        return _Resp(not url.endswith("sendPhoto"))

    monkeypatch.setattr(engine, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(engine.requests, "post", fake_post)
    engine._telegram_send("STEAL x\nbody", "https://v/1", "https://img/1.jpg")
    assert [c[0] for c in calls] == ["sendPhoto", "sendMessage"]
    assert '"url": "https://v/1"' in calls[1][1]["reply_markup"]
