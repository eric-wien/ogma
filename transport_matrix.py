#!/usr/bin/env python3
"""
Matrix transport for the Ogma gateway — every Matrix-specific detail lives here.

Implements the same surface as transport_telegram.py:

    validate()               -> (ok, info)              credential sanity check
    register_menu(commands)  -> None                    command list -> room topics
    updates()                -> yields {"chat_id", "from_id", "actor", "text", "date"}
    send(chat_id, text)      -> None                    chunked, with one retry
    send_document(chat_id, filename, content, caption) -> None   long output as a file
    typing(chat_id)          -> None                    best-effort activity indicator

Speaks the plain Matrix client-server API (long-polling /sync) against your own
homeserver over TLS — no third party sees the channel, which is the point of
this backend. End-to-end encryption is NOT implemented: E2EE needs olm and a
device/session store, which would break the zero-dependency rule. Run the bot
in unencrypted rooms on a homeserver you trust (self-hosted, federation off).

Matrix quirks kept on this side of the seam:
- chat_id is a room id ("!abc:server"); the sender is always a distinct user id,
  so "actor" (who the gateway authorizes) is always the sender — rooms are
  groups by nature, authorizing the room would hand the runner to every member.
- /sync is resumed via a since-token persisted in state/; on the very first run
  the backlog is skipped so old messages are never replayed as fresh commands
  (at-most-once on restart beats re-running a stale /restart).
- Room invites are auto-joined, but only when the inviter is in
  MATRIX_ALLOWED_USERS / MATRIX_GUEST_USERS — on an open homeserver a stranger
  must not be able to drag the bot into rooms.

Zero third-party dependencies: Python standard library only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path


def log(*a: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


MSG_MAX = 4000  # chars per message event; events cap at 64 KiB, this is far below

# Server-side filter: only room timelines matter to a command bot — presence,
# receipts, typing and account data are noise on a long-poll.
SYNC_FILTER = json.dumps({
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {
        "ephemeral": {"types": []},
        "account_data": {"types": []},
        "state": {"lazy_load_members": True},
        "timeline": {"limit": 50},
    },
})


def _q(s: str) -> str:
    """Path-quote a Matrix identifier (room/user ids contain '!', '@', ':')."""
    return urllib.parse.quote(s, safe="")


def _build_topic(commands: list[tuple[str, str]]) -> str:
    """One-line command reference for a room topic (names arrive slash-less)."""
    return ("Commands: " + " ".join("/" + c for c, _ in commands)
            + " | /help for details")


class MatrixTransport:
    name = "matrix"

    def __init__(self, homeserver: str, user_id: str, access_token: str,
                 state_dir: Path) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.user_id = user_id
        self.token = access_token
        self.since_file = state_dir / "matrix_sync.since"
        self._menu_topic = ""  # set by register_menu; "" until then
        # Inviters the bot follows into rooms: exactly the people the gateway
        # would listen to anyway.
        self.trusted_inviters = {
            x.strip()
            for var in ("MATRIX_ALLOWED_USERS", "MATRIX_GUEST_USERS")
            for x in os.environ.get(var, "").split(",") if x.strip()
        }

    def _call(self, method: str, path: str, body: bytes | None = None,
              content_type: str = "application/json", timeout: int = 60) -> dict:
        req = urllib.request.Request(
            f"{self.homeserver}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def _json(self, method: str, path: str, payload: dict,
              timeout: int = 60) -> dict:
        return self._call(method, path, json.dumps(payload).encode(),
                          timeout=timeout)

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------
    def validate(self) -> tuple[bool, str]:
        """Check the access token via /whoami so a revoked token is an obvious
        one-line log, not an endless stream of 401s from /sync."""
        try:
            r = self._call("GET", "/_matrix/client/v3/account/whoami", timeout=15)
        except Exception as e:  # noqa: BLE001
            return (False, str(e))
        got = r.get("user_id", "")
        if not got:
            return (False, str(r))
        if self.user_id and got != self.user_id:
            log(f"⚠️ MATRIX_USER_ID={self.user_id} but token belongs to {got} — using {got}")
        self.user_id = got
        return (True, got.lstrip("@"))

    def _set_topic(self, room_id: str) -> bool:
        """Publish the command reference as the room topic. Skips the PUT when
        the topic already matches — a state event per restart would litter the
        timeline with 'changed the topic' notices. Needs power level >= 50 in
        the room; failures (403 in rooms where the bot is a plain member) are
        logged, never fatal."""
        if not self._menu_topic:
            return False
        path = f"/_matrix/client/v3/rooms/{_q(room_id)}/state/m.room.topic/"
        try:
            if self._call("GET", path).get("topic") == self._menu_topic:
                return True
        except Exception:  # noqa: BLE001 — 404 = no topic yet; just set it
            pass
        try:
            self._json("PUT", path, {"topic": self._menu_topic})
            return True
        except Exception as e:  # noqa: BLE001
            log(f"matrix: could not set topic in {room_id}:", e)
            return False

    def register_menu(self, commands: list[tuple[str, str]]) -> None:
        """Element (X) has no bot-command menu UI, so the room topic is the
        persistent, always-visible command reference. Called on every gateway
        startup, so the topic follows command changes automatically."""
        self._menu_topic = _build_topic(commands)
        try:
            rooms = self._call(
                "GET", "/_matrix/client/v3/joined_rooms").get("joined_rooms", [])
        except Exception as e:  # noqa: BLE001
            log("matrix: joined_rooms failed, command topic not set:", e)
            return
        n = sum(self._set_topic(r) for r in rooms)
        log(f"matrix: command topic current in {n}/{len(rooms)} room(s)")

    # -----------------------------------------------------------------------
    # Receiving
    # -----------------------------------------------------------------------
    def _load_since(self) -> str:
        try:
            return self.since_file.read_text().strip()
        except OSError:
            return ""

    def _save_since(self, token: str) -> None:
        try:
            self.since_file.parent.mkdir(exist_ok=True)
            self.since_file.write_text(token)
        except OSError as e:
            log("matrix: could not persist sync token:", e)

    def _sync(self, since: str, timeout_ms: int) -> dict:
        params = {"filter": SYNC_FILTER, "timeout": str(timeout_ms)}
        if since:
            params["since"] = since
        return self._call(
            "GET", "/_matrix/client/v3/sync?" + urllib.parse.urlencode(params),
            timeout=timeout_ms // 1000 + 10,
        )

    def _handle_invites(self, resp: dict) -> None:
        for room_id, room in (resp.get("rooms", {}).get("invite") or {}).items():
            inviter = next(
                (ev.get("sender", "") for ev in
                 (room.get("invite_state", {}).get("events") or [])
                 if ev.get("type") == "m.room.member"
                 and ev.get("state_key") == self.user_id), "")
            if inviter not in self.trusted_inviters:
                log(f"matrix: ignoring invite to {room_id} from {inviter or '?'} "
                    "(not in MATRIX_ALLOWED_USERS/MATRIX_GUEST_USERS)")
                continue
            try:
                self._json("POST", f"/_matrix/client/v3/join/{_q(room_id)}", {})
                log(f"matrix: joined {room_id} (invited by {inviter})")
                self._set_topic(room_id)
            except Exception as e:  # noqa: BLE001
                log(f"matrix: join {room_id} failed:", e)

    def updates(self) -> Iterator[dict]:
        """Long-poll /sync forever, yielding one dict per text message.

        chat_id is the room to reply to; actor is the sender (always — see
        module docstring); date is the homeserver's origin_server_ts in seconds,
        which lets the gateway judge time-sensitive replies (/confirm) by when
        the user actually sent them. Transport errors are logged and retried
        here; the gateway never sees them.

        The since-token is persisted after each batch: a message the gateway
        crashed on is dropped on restart, never replayed.
        """
        since = self._load_since()
        if not since:
            # First run: fetch only the position marker, never the backlog —
            # yesterday's "/restart" must not run today.
            try:
                since = self._sync("", 0).get("next_batch", "")
                self._save_since(since)
                log("matrix: initial sync — backlog skipped, listening from now")
            except Exception as e:  # noqa: BLE001
                log("matrix: initial sync failed:", e)
                time.sleep(3)
        while True:
            try:
                resp = self._sync(since, 25000) if since else self._sync("", 0)
            except Exception as e:  # noqa: BLE001
                log("matrix: sync error:", e)
                time.sleep(3)
                continue
            self._handle_invites(resp)
            for room_id, room in (resp.get("rooms", {}).get("join") or {}).items():
                for ev in room.get("timeline", {}).get("events") or []:
                    if (ev.get("type") != "m.room.message"
                            or ev.get("sender") == self.user_id):
                        continue
                    content = ev.get("content") or {}
                    if content.get("msgtype") != "m.text":
                        continue
                    yield {
                        "chat_id": room_id,
                        "from_id": ev.get("sender", ""),
                        "actor": ev.get("sender", ""),
                        "text": str(content.get("body", "")),
                        "date": int(ev.get("origin_server_ts", 0)) // 1000,
                    }
            new_since = resp.get("next_batch", "")
            if new_since and new_since != since:
                since = new_since
                self._save_since(since)

    # -----------------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------------
    def _send_event(self, room_id: str, content: dict) -> None:
        txn = uuid.uuid4().hex
        self._json("PUT",
                   f"/_matrix/client/v3/rooms/{_q(room_id)}/send/m.room.message/{txn}",
                   content)

    def send(self, chat_id: str, text: str) -> None:
        # Split long replies on line/space boundaries.
        while text:
            chunk, text = text[:MSG_MAX], text[MSG_MAX:]
            if text:
                cut = max(chunk.rfind("\n"), chunk.rfind(" "))
                if cut > MSG_MAX // 2:
                    text, chunk = chunk[cut:] + text, chunk[:cut]
            for attempt in (1, 2):  # one retry — a silently dropped reply looks like a dead bot
                try:
                    self._send_event(chat_id, {"msgtype": "m.text", "body": chunk})
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        log("matrix: send failed (giving up):", e)
                        return
                    log("matrix: send failed (retrying):", e)
                    time.sleep(2)

    def send_document(self, chat_id: str, filename: str, content: str,
                      caption: str = "") -> None:
        """Deliver long text as a file: upload to the media repo, then reference
        it from an m.file message. Falls back to chunked send() if either step
        fails — a degraded reply beats a dropped one.
        """
        data = content.encode()
        try:
            uri = self._call(
                "POST",
                "/_matrix/media/v3/upload?" + urllib.parse.urlencode(
                    {"filename": filename}),
                data, content_type="text/plain; charset=utf-8", timeout=120,
            ).get("content_uri", "")
            if not uri:
                raise ValueError("upload returned no content_uri")
            if caption:
                self._send_event(chat_id, {"msgtype": "m.text", "body": caption})
            self._send_event(chat_id, {
                "msgtype": "m.file", "body": filename, "filename": filename,
                "url": uri,
                "info": {"mimetype": "text/plain", "size": len(data)},
            })
        except Exception as e:  # noqa: BLE001
            log("matrix: file upload failed, falling back to chunks:", e)
            self.send(chat_id, content)

    def typing(self, chat_id: str) -> None:
        try:
            self._json("PUT",
                       f"/_matrix/client/v3/rooms/{_q(chat_id)}/typing/{_q(self.user_id)}",
                       {"typing": True, "timeout": 6000}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
