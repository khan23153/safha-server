import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify_telegram as tg  # noqa: E402


def test_no_capacity_is_silent(monkeypatch):
    monkeypatch.setenv("STATUS", "no_capacity")
    text, silent = tg.message()
    assert "capacity nahi mili" in text and silent
    assert tg.message("no_capacity", "", "7")[0].startswith("⏳ Koshish #7")


def test_created_rings_with_ip(monkeypatch):
    monkeypatch.setenv("STATUS", "created")
    monkeypatch.setenv("PUBLIC_IP", "203.0.113.7")
    text, silent = tg.message()
    assert "203.0.113.7" in text and not silent


def test_error_rings_with_reason(monkeypatch):
    monkeypatch.setenv("STATUS", "error")
    monkeypatch.setenv("ERROR", "Oracle API 401 NotAuthenticated")
    text, silent = tg.message()
    assert "NotAuthenticated" in text and not silent


def test_without_token_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(tg, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    tg.main()
