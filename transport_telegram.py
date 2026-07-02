#!/usr/bin/env python3
"""
Telegram transport for the Ogma gateway — every Telegram-specific detail lives here.

The gateway talks to chat through this small surface only:

    validate()               -> (ok, bot_username)     token sanity check
    register_menu(commands)  -> None                   slash-command menu (optional capability)
    updates()                -> yields {"chat_id", "from_id", "text"}
    send(chat_id, text)      -> None                   chunked, with one retry
    send_document(chat_id, filename, content, caption) -> None   long output as a file
    typing(chat_id)          -> None                   best-effort activity indicator

A future transport (e.g. Signal via signal-cli) implements the same surface and the
gateway shouldn't need to change. Keep protocol quirks (UTF-16 length rule, 4096
limit, getUpdates offsets, multipart uploads) on this side of the seam.

Zero third-party dependencies: Python standard library only.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


TG_MAX = 4000  # Telegram hard limit is 4096 UTF-16 units; leave headroom
CAPTION_MAX = 1000  # documents: Telegram caps captions at 1024


def _tg_len(s: str) -> int:
    """Telegram's 4096 limit counts UTF-16 code units — emoji count double."""
    return len(s.encode("utf-16-le")) // 2


class TelegramTransport:
    name = "telegram"

    def __init__(self, token: str) -> None:
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, params: dict, timeout: int = 60) -> dict:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.api}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------
    def validate(self) -> tuple[bool, str]:
        """Check the bot token via getMe so a bad token is an obvious one-line log,
        not an endless stream of 401s from getUpdates."""
        try:
            r = self._call("getMe", {}, timeout=15)
        except Exception as e:  # noqa: BLE001
            return (False, str(e))
        if r.get("ok"):
            return (True, (r.get("result") or {}).get("username", "?"))
        return (False, r.get("description", "not ok"))

    def register_menu(self, commands: list[tuple[str, str]]) -> None:
        """Register the slash-command menu with Telegram (server-side; idempotent).

        A chat resolves its command list most-specific-scope-first, so we register at
        BOTH `default` (groups / fallback) AND `all_private_chats`. The latter is
        required: any commands previously set at the private-chats scope would otherwise
        shadow the default-scope list and the menu would show a stale set in DMs.
        """
        payload = json.dumps([{"command": c, "description": d} for c, d in commands])
        for scope in (None, {"type": "all_private_chats"}):
            params = {"commands": payload}
            if scope:
                params["scope"] = json.dumps(scope)
            label = scope["type"] if scope else "default"
            try:
                r = self._call("setMyCommands", params, timeout=15)
                log(f"menu registered ({label})" if r.get("ok")
                    else f"menu register failed ({label}): {r.get('description')}")
            except Exception as e:  # noqa: BLE001
                log(f"setMyCommands error ({label}):", e)

    # -----------------------------------------------------------------------
    # Receiving
    # -----------------------------------------------------------------------
    def updates(self) -> Iterator[dict]:
        """Long-poll getUpdates forever, yielding one dict per text message.

        chat_id is where to reply; from_id is who wrote it (differs from chat_id
        in groups — the gateway authorizes on the sender there). Transport errors
        are logged and retried here; the gateway never sees them.
        """
        offset = 0
        while True:
            try:
                resp = self._call("getUpdates", {"offset": offset, "timeout": 50},
                                  timeout=70)
            except Exception as e:  # noqa: BLE001
                log("getUpdates error:", e)
                time.sleep(5)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg or "text" not in msg:
                    continue
                yield {
                    "chat_id": str(msg["chat"]["id"]),
                    "from_id": str((msg.get("from") or {}).get("id") or ""),
                    "text": msg["text"],
                }

    # -----------------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------------
    def send(self, chat_id: str, text: str) -> None:
        # Split long replies on line/space boundaries.
        while text:
            chunk, text = text[:TG_MAX], text[TG_MAX:]
            if text:
                cut = max(chunk.rfind("\n"), chunk.rfind(" "))
                if cut > TG_MAX // 2:
                    text, chunk = chunk[cut:] + text, chunk[:cut]
            while _tg_len(chunk) > TG_MAX:
                text, chunk = chunk[-200:] + text, chunk[:-200]
            for attempt in (1, 2):  # one retry — a silently dropped reply looks like a dead bot
                try:
                    self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        log("sendMessage failed (giving up):", e)
                        return
                    log("sendMessage failed (retrying):", e)
                    time.sleep(2)

    def send_document(self, chat_id: str, filename: str, content: str,
                      caption: str = "") -> None:
        """Deliver long text as an attached file instead of a wall of chunks.

        Falls back to chunked send() if the upload fails — a degraded reply beats
        a dropped one.
        """
        boundary = f"----ogma{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )

        field("chat_id", chat_id)
        if caption:
            field("caption", caption[:CAPTION_MAX])
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/plain; charset=utf-8"
            "\r\n\r\n".encode() + content.encode() + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"{self.api}/sendDocument", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return
        except Exception as e:  # noqa: BLE001
            log("sendDocument failed, falling back to chunks:", e)
        self.send(chat_id, content)

    def typing(self, chat_id: str) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001
            pass
