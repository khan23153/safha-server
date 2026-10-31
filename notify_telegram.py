#!/usr/bin/env python3
"""Tell the owner on Telegram how the latest server-grab run went.

"No capacity" messages are silent (no sound); a created server or an error
rings. Without TELEGRAM_BOT_TOKEN this does nothing. Without TELEGRAM_CHAT_ID
it uses the chat that last messaged the bot (send it /start once) and prints
that chat id as a notice so it can be saved as a secret.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, STATUS, PUBLIC_IP, ATTEMPTS, ERROR,
GITHUB_RUN_NUMBER, GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def find_chat(token: str) -> str:
    updates = call(token, "getUpdates").get("result", [])
    for update in reversed(updates):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat", {})
        if chat.get("id") is not None:
            print(f"::notice title=Telegram chat id::{chat['id']} (save as TELEGRAM_CHAT_ID)")
            return str(chat["id"])
    return ""


def message() -> tuple[str, bool]:
    """(text, silent) for the run's outcome."""
    status = os.environ.get("STATUS") or "error"
    ip = os.environ.get("PUBLIC_IP", "")
    attempts = os.environ.get("ATTEMPTS") or "?"
    run = os.environ.get("GITHUB_RUN_NUMBER", "?")
    link = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
    if status == "created":
        return (f"✅ Safha server ban gaya!\nIP: {ip}\n"
                f"SSH: ssh -i safha-server-key ubuntu@{ip}\n"
                "Setup ~10 min mein khud chalega. Bot ab band ho gaya.", False)
    if status == "exists":
        return f"ℹ️ Safha server pehle se bana hua hai. IP: {ip}", True
    if status == "no_capacity":
        return (f"⏳ Run #{run}: Mumbai mein capacity nahi mili "
                f"({attempts} koshish). Agli koshish ~2-3 min mein.", True)
    if status == "not_configured":
        return "", True
    error = os.environ.get("ERROR") or "unknown error"
    return f"❌ Run #{run} fail hua: {error}\n{link}", False


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("No TELEGRAM_BOT_TOKEN; skipping Telegram.")
        return
    text, silent = message()
    if not text:
        return
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or find_chat(token)
    if not chat:
        print("::warning title=Telegram::No chat found. Send /start to the bot once.")
        return
    try:
        call(token, "sendMessage", chat_id=chat, text=text,
             disable_notification="true" if silent else "false",
             disable_web_page_preview="true")
    except Exception as err:  # a notification must never fail the grab
        print(f"::warning title=Telegram send failed::{type(err).__name__}")
        sys.exit(0)


if __name__ == "__main__":
    main()
