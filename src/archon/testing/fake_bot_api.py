"""A local Telegram Bot API server that aiogram polls for real.

Point ``Settings.telegram_api_base`` at :attr:`FakeBotApi.base_url` and every
``make_bot`` bot talks to this process over loopback: ``getMe``, long-polled
``getUpdates``, every send/edit/answer method, ``getFile`` and file downloads.
Each request is recorded in :attr:`calls` (method + decoded form fields), and
:meth:`inject` feeds an update — a Business message, a deletion, an inline
button tap, an owner command — into the poll queue so the real dispatcher,
handlers, confirm gate and sinks all execute.

Refusals are encoded where they matter: a stale ``callback_query`` id, a
``business_connection_id`` the server does not know, a scripted 429 with
``retry_after`` (the flood control the live bot has hit), and a scripted
dropped connection (the ``Connector is closed`` failure mode). A fake that is
more permissive than api.telegram.org hides exactly the bugs this suite
exists to find.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

OWNER_ID = 1
BOT_ID = 8000000001


@dataclass(slots=True)
class Call:
    method: str
    data: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    #: False when the server refused the call (scripted 429/drop or an API error).
    ok: bool = True

    def json(self, key: str) -> Any:
        """A field aiogram serialised as JSON (reply_markup, entities, …)."""
        raw = self.data.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw


class FakeBotApi:
    def __init__(self, *, bot_username: str = "archon_test_bot") -> None:
        self.bot_username = bot_username
        self.calls: list[Call] = []
        self.updates: list[dict[str, Any]] = []
        self.messages: dict[tuple[int, int], dict[str, Any]] = {}
        self.known_connections: set[str] = set()
        self.valid_callback_ids: set[str] = set()
        self.fail_next: list[dict[str, Any]] = []   # scripted refusals, FIFO
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(100)
        self._file_ids = itertools.count(1)
        self._files: dict[str, bytes] = {}
        self._wake = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.port = 0
        self.token: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> FakeBotApi:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._handle)
        app.router.add_get("/file/bot{token}/{path:.*}", self._file)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        self._wake.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- scripting the server's mood --------------------------------------------

    def refuse_next(self, method: str, *, status: int = 429, retry_after: int = 1,
                    description: str | None = None, drop: bool = False) -> None:
        """Make the next call to ``method`` fail. ``drop`` closes the connection
        instead of answering (aiohttp then raises a network error)."""
        self.fail_next.append({"method": method, "status": status,
                               "retry_after": retry_after, "drop": drop,
                               "description": description})

    # --- injecting updates -------------------------------------------------------

    def inject(self, update: dict[str, Any]) -> int:
        update_id = next(self._update_ids)
        self.updates.append({"update_id": update_id, **update})
        self._wake.set()
        return update_id

    def owner_message(self, text: str, *, chat_id: int = OWNER_ID,
                      from_id: int = OWNER_ID, first_name: str = "Owner") -> int:
        """A message from ``from_id`` in the private chat with the bot."""
        return self.inject({"message": self._message(chat_id, from_id, text,
                                                     first_name=first_name)})

    def owner_document(self, file_name: str, content: bytes, *, chat_id: int = OWNER_ID) -> int:
        file_id = f"file{next(self._file_ids)}"
        self._files[file_id] = content
        msg = self._message(chat_id, OWNER_ID, None)
        msg["document"] = {"file_id": file_id, "file_unique_id": file_id,
                           "file_name": file_name, "file_size": len(content)}
        return self.inject({"message": msg})

    def business_connection(self, connection_id: str, *, user_id: int = OWNER_ID,
                            enabled: bool = True, can_reply: bool = True) -> int:
        self.known_connections.add(connection_id)
        return self.inject({"business_connection": {
            "id": connection_id,
            "user": self._user(user_id, "Owner"),
            "user_chat_id": user_id,
            "date": int(time.time()),
            "is_enabled": enabled,
            "rights": {"can_reply": can_reply, "can_read_messages": True,
                       "can_delete_sent_messages": True},
        }})

    def business_message(self, connection_id: str, *, chat_id: int, from_id: int,
                         text: str | None = None, message_id: int | None = None,
                         first_name: str = "Dana", photo: bool = False,
                         edited: bool = False) -> int:
        msg = self._message(chat_id, from_id, text, first_name=first_name,
                            message_id=message_id)
        msg["business_connection_id"] = connection_id
        if photo:
            file_id = f"photo{next(self._file_ids)}"
            self._files[file_id] = b"\xff\xd8fake-jpeg"
            msg["photo"] = [{"file_id": file_id, "file_unique_id": file_id,
                             "width": 640, "height": 480, "file_size": 1024}]
        key = "edited_business_message" if edited else "business_message"
        return self.inject({key: msg})

    def business_deleted(self, connection_id: str, *, chat_id: int,
                         message_ids: list[int], first_name: str = "Dana") -> int:
        return self.inject({"deleted_business_messages": {
            "business_connection_id": connection_id,
            "chat": self._chat(chat_id, first_name),
            "message_ids": list(message_ids),
        }})

    def callback(self, *, data: str, message: dict[str, Any] | None = None,
                 from_id: int = OWNER_ID) -> int:
        """A tap on an inline button. ``message`` is the bot's message carrying
        the keyboard (as returned by :meth:`sent_message`)."""
        cid = f"cb{next(self._file_ids)}"
        self.valid_callback_ids.add(cid)
        return self.inject({"callback_query": {
            "id": cid, "from": self._user(from_id, "Owner"),
            "chat_instance": "ci1", "data": data, "message": message,
        }})

    # --- reading what the bot did ---------------------------------------------------

    def calls_of(self, method: str, **match: Any) -> list[Call]:
        out = []
        for c in self.calls:
            if c.method != method:
                continue
            if all(str(c.data.get(k)) == str(v) for k, v in match.items()):
                out.append(c)
        return out

    def texts(self, method: str = "sendMessage", chat_id: int | None = None) -> list[str]:
        """Texts the server ACCEPTED (refused attempts are in ``calls`` with ok=False)."""
        return [c.data.get("text", "") for c in self.calls_of(method)
                if c.ok and (chat_id is None or str(c.data.get("chat_id")) == str(chat_id))]

    async def wait_for_call(self, method: str, *, timeout: float = 5.0, count: int = 1,
                            **match: Any) -> Call:
        deadline = time.monotonic() + timeout
        while True:
            found = self.calls_of(method, **match)
            if len(found) >= count:
                return found[-1]
            if time.monotonic() > deadline:
                seen = [c.method for c in self.calls][-15:]
                raise AssertionError(
                    f"bot never called {method} matching {match} within {timeout}s; "
                    f"recent: {seen}")
            await asyncio.sleep(0.02)

    def sent_message(self, call: Call) -> dict[str, Any]:
        """The Message object the server returned for a send — what a user's
        client would show, and what a callback_query references."""
        mid = call.data.get("_message_id")
        return self.messages[(int(call.data["chat_id"]), int(mid))]

    # --- HTTP ---------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        method = request.match_info["method"]
        self.token = request.match_info["token"]
        data: dict[str, Any] = {}
        files: dict[str, bytes] = {}
        if request.content_type.startswith("multipart/") or \
                request.content_type == "application/x-www-form-urlencoded":
            post = await request.post()
            for key, value in post.items():
                if isinstance(value, web.FileField):
                    files[key] = value.file.read()
                else:
                    data[key] = value
        elif request.can_read_body:
            try:
                data = json.loads(await request.text() or "{}")
            except ValueError:
                data = {}
        call = Call(method=method, data=data, files=files)
        self.calls.append(call)

        for scripted in list(self.fail_next):
            if scripted["method"] == method:
                self.fail_next.remove(scripted)
                call.ok = False
                if scripted["drop"]:
                    request.transport.close()  # type: ignore[union-attr]
                    return web.Response(status=500)
                body = {"ok": False, "error_code": scripted["status"],
                        "description": scripted["description"] or "Too Many Requests: retry after",
                        "parameters": {"retry_after": scripted["retry_after"]}}
                return web.json_response(body, status=scripted["status"])

        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return self._ok(True)
        try:
            result = await handler(call)
        except _ApiError as exc:
            call.ok = False
            return web.json_response({"ok": False, "error_code": exc.code,
                                      "description": exc.description}, status=exc.code)
        return self._ok(result)

    async def _file(self, request: web.Request) -> web.Response:
        file_id = request.match_info["path"].rsplit("/", 1)[-1]
        content = self._files.get(file_id)
        if content is None:
            return web.Response(status=404)
        return web.Response(body=content)

    @staticmethod
    def _ok(result: Any) -> web.Response:
        return web.json_response({"ok": True, "result": result})

    # --- methods ------------------------------------------------------------------------

    async def _m_getMe(self, call: Call) -> dict[str, Any]:
        return {"id": BOT_ID, "is_bot": True, "first_name": "Archon",
                "username": self.bot_username, "can_join_groups": True,
                "can_read_all_group_messages": False, "supports_inline_queries": False}

    async def _m_getUpdates(self, call: Call) -> list[dict[str, Any]]:
        offset = int(call.data.get("offset") or 0)
        timeout = float(call.data.get("timeout") or 0)
        deadline = time.monotonic() + timeout
        while True:
            pending = [u for u in self.updates if u["update_id"] >= offset]
            if pending:
                return pending[:100]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(remaining, 0.25))
            except TimeoutError:
                pass

    async def _m_deleteWebhook(self, call: Call) -> bool:
        return True

    async def _m_getWebhookInfo(self, call: Call) -> dict[str, Any]:
        return {"url": "", "has_custom_certificate": False, "pending_update_count": 0}

    async def _m_setMyCommands(self, call: Call) -> bool:
        return True

    async def _m_sendChatAction(self, call: Call) -> bool:
        return True

    async def _m_answerCallbackQuery(self, call: Call) -> bool:
        cid = call.data.get("callback_query_id")
        if cid not in self.valid_callback_ids:
            raise _ApiError(400, "Bad Request: query is too old and response timeout expired or query ID is invalid")
        self.valid_callback_ids.discard(cid)
        return True

    async def _m_sendMessage(self, call: Call) -> dict[str, Any]:
        return self._store_outgoing(call, "text")

    async def _m_sendPhoto(self, call: Call) -> dict[str, Any]:
        return self._store_outgoing(call, "photo")

    async def _m_sendVideo(self, call: Call) -> dict[str, Any]:
        return self._store_outgoing(call, "video")

    async def _m_sendAudio(self, call: Call) -> dict[str, Any]:
        return self._store_outgoing(call, "audio")

    async def _m_sendDocument(self, call: Call) -> dict[str, Any]:
        return self._store_outgoing(call, "document")

    async def _m_editMessageText(self, call: Call) -> dict[str, Any]:
        key = (int(call.data["chat_id"]), int(call.data["message_id"]))
        msg = self.messages.get(key)
        if msg is None:
            raise _ApiError(400, "Bad Request: message to edit not found")
        msg["text"] = call.data.get("text", "")
        return msg

    async def _m_deleteMessage(self, call: Call) -> bool:
        key = (int(call.data["chat_id"]), int(call.data["message_id"]))
        return self.messages.pop(key, None) is not None

    async def _m_getFile(self, call: Call) -> dict[str, Any]:
        file_id = call.data["file_id"]
        if file_id not in self._files:
            raise _ApiError(400, "Bad Request: invalid file_id")
        return {"file_id": file_id, "file_unique_id": file_id,
                "file_size": len(self._files[file_id]), "file_path": f"files/{file_id}"}

    # --- helpers --------------------------------------------------------------------------

    def _store_outgoing(self, call: Call, kind: str) -> dict[str, Any]:
        conn = call.data.get("business_connection_id")
        if conn and conn not in self.known_connections:
            raise _ApiError(400, "Bad Request: BUSINESS_CONNECTION_INVALID")
        chat_id = int(call.data["chat_id"])
        mid = next(self._message_ids)
        call.data["_message_id"] = mid
        msg: dict[str, Any] = {
            "message_id": mid, "date": int(time.time()),
            "chat": self._chat(chat_id, "Owner" if chat_id > 0 else "Channel"),
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Archon",
                     "username": self.bot_username},
        }
        if kind == "text":
            msg["text"] = call.data.get("text", "")
        else:
            msg["caption"] = call.data.get("caption", "")
            msg[kind] = [{"file_id": f"out{mid}", "file_unique_id": f"out{mid}",
                          "width": 1, "height": 1}] if kind == "photo" else \
                {"file_id": f"out{mid}", "file_unique_id": f"out{mid}"}
        markup = call.json("reply_markup")
        if markup:
            msg["reply_markup"] = markup
        if conn:
            msg["business_connection_id"] = conn
        self.messages[(chat_id, mid)] = msg
        return msg

    @staticmethod
    def _user(user_id: int, first_name: str) -> dict[str, Any]:
        return {"id": user_id, "is_bot": False, "first_name": first_name,
                "username": f"user{user_id}"}

    @staticmethod
    def _chat(chat_id: int, first_name: str) -> dict[str, Any]:
        if chat_id > 0:
            return {"id": chat_id, "type": "private", "first_name": first_name,
                    "username": f"user{chat_id}"}
        return {"id": chat_id, "type": "channel" if str(chat_id).startswith("-100") else "group",
                "title": first_name}

    def _message(self, chat_id: int, from_id: int, text: str | None, *,
                 first_name: str = "Owner", message_id: int | None = None) -> dict[str, Any]:
        mid = message_id or next(self._message_ids)
        msg: dict[str, Any] = {
            "message_id": mid, "date": int(time.time()),
            "chat": self._chat(chat_id, first_name),
            "from": self._user(from_id, first_name),
        }
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                cmd = text.split()[0]
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        return msg


class _ApiError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
