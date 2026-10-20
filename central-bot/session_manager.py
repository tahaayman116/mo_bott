"""Manages MANY phone sessions, ONE connected at a time.

One API_ID/API_HASH pair drives all numbers (app keys, not account keys).
Each phone gets its own .session file under SESSIONS_DIR.

Flows:
  add:    start_login(phone) -> code sent to the phone
          submit_code(phone, code) -> ok | need_password | error
          submit_password(phone, pwd) -> ok | error
  switch: set_active(phone) -> disconnect current, connect chosen
  temp:   temp_client(phone) -> connect any saved session briefly
          (used to collect dialogs from a non-active number)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

import database


class LoginError(Exception):
    pass


class SessionManager:
    def __init__(self, api_id: int, api_hash: str, sessions_dir: Path,
                 db_path: Path):
        self.api_id = api_id
        self.api_hash = api_hash
        self.sessions_dir = sessions_dir
        self.db_path = db_path
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._active_phone = ""
        self._active_client: TelegramClient | None = None
        self._pending: dict[str, dict] = {}  # phone -> {client, phone_code_hash}

    # ---- paths ----
    def session_path(self, phone: str) -> Path:
        safe = phone.replace("+", "plus")
        return self.sessions_dir / f"{safe}.session"

    def _new_client(self, phone: str) -> TelegramClient:
        return TelegramClient(str(self.session_path(phone).with_suffix("")),
                              self.api_id, self.api_hash,
                              request_retries=4, connection_retries=4,
                              retry_delay=2, timeout=20)

    @staticmethod
    async def _connect(client: TelegramClient) -> None:
        """connect() with a hard cap so slow networks fail instead of hang."""
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except (asyncio.TimeoutError, TimeoutError):
            try:
                await client.disconnect()
            except Exception:
                pass
            raise LoginError("الشبكة بطيئة والاتصال مقفش — جرب تاني أو غير النت.")

    # ---- login flow ----
    async def start_login(self, phone: str) -> str:
        """Send the verification code. Returns 'sent'. Raises LoginError."""
        async with self._lock:
            if database.get_account(self.db_path, phone):
                raise LoginError("الرقم ده متسجل أصلًا.")
            if phone in self._pending:
                try:
                    await self._pending[phone]["client"].disconnect()
                except Exception:
                    pass
                self._pending.pop(phone, None)
            client = self._new_client(phone)
            try:
                await self._connect(client)
                try:
                    sent = await client.send_code_request(phone)
                except PhoneNumberInvalidError:
                    raise LoginError("الرقم غلط — اتأكد منه (+كود البلد).")
                except FloodWaitError as e:
                    raise LoginError(f"تليجرام طلب انتظار {e.seconds} ثانية — جرّب كمان شوية.")
                self._pending[phone] = {"client": client,
                                        "phone_code_hash": sent.phone_code_hash}
                return "sent"
            except LoginError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise LoginError(f"مش قادر أبعت الكود ({type(e).__name__}). اتأكد من النت.")

    async def submit_code(self, phone: str, code: str) -> str:
        """Returns 'ok' | 'need_password'. Raises LoginError."""
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        async with self._lock:
            pend = self._pending.get(phone)
            if not pend:
                raise LoginError("مفيش تسجيل شغال للرقم ده — ابدأ من الأول.")
            client = pend["client"]
            try:
                await client.sign_in(phone=phone, code=code,
                                     phone_code_hash=pend["phone_code_hash"])
            except SessionPasswordNeededError:
                return "need_password"
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                raise LoginError("الكود غلط أو انتهى — اطلب كود جديد.")
            except FloodWaitError as e:
                raise LoginError(f"انتظار {e.seconds} ثانية من تليجرام.")
            except Exception as e:
                raise LoginError(f"فشل تسجيل الدخول ({type(e).__name__}).")
            me = await client.get_me()
            name = " ".join(x for x in
                            [getattr(me, "first_name", ""), getattr(me, "last_name", "")]
                            if x)
            database.add_account(self.db_path, phone,
                                 str(self.session_path(phone)), name)
            self._pending.pop(phone, None)
            try:
                await client.disconnect()
            except Exception:
                pass
            # first account becomes active automatically
            if not database.get_active_phone(self.db_path):
                database.set_active_phone(self.db_path, phone)
            return "ok"

    async def submit_password(self, phone: str, password: str) -> str:
        """Returns 'ok'. Raises LoginError."""
        async with self._lock:
            pend = self._pending.get(phone)
            if not pend:
                raise LoginError("مفيش تسجيل شغال للرقم ده — ابدأ من الأول.")
            client = pend["client"]
            try:
                await client.sign_in(password=password or "")
            except FloodWaitError as e:
                raise LoginError(f"انتظار {e.seconds} ثانية من تليجرام.")
            except Exception:
                raise LoginError("الباسورد غلط — جرّب تاني.")
            me = await client.get_me()
            name = " ".join(x for x in
                            [getattr(me, "first_name", ""), getattr(me, "last_name", "")]
                            if x)
            database.add_account(self.db_path, phone,
                                 str(self.session_path(phone)), name)
            self._pending.pop(phone, None)
            try:
                await client.disconnect()
            except Exception:
                pass
            if not database.get_active_phone(self.db_path):
                database.set_active_phone(self.db_path, phone)
            return "ok"

    async def cancel_login(self, phone: str) -> None:
        async with self._lock:
            pend = self._pending.pop(phone, None)
            if pend:
                try:
                    await pend["client"].disconnect()
                except Exception:
                    pass

    # ---- active session (exactly one connected) ----
    @property
    def active_phone(self) -> str:
        return self._active_phone or database.get_active_phone(self.db_path)

    @property
    def active_client(self):
        return self._active_client

    async def set_active(self, phone: str) -> str:
        """Disconnect current, connect chosen. Returns display name."""
        async with self._lock:
            acc = database.get_account(self.db_path, phone)
            if not acc:
                raise LoginError("الرقم ده مش متسجل.")
            if self._active_client is not None:
                try:
                    await self._active_client.disconnect()
                except Exception:
                    pass
                self._active_client = None
            client = self._new_client(phone)
            try:
                await self._connect(client)
                if not await client.is_user_authorized():
                    raise LoginError("الجلسة انتهت — امسح الرقم وسجّله تاني.")
                me = await client.get_me()
                name = " ".join(x for x in
                                [getattr(me, "first_name", ""), getattr(me, "last_name", "")]
                                if x) or phone
            except LoginError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise LoginError(f"مش قادر أوصل ({type(e).__name__}).")
            self._active_client = client
            self._active_phone = phone
            database.set_active_phone(self.db_path, phone)
            database.touch_account(self.db_path, phone)
            return name

    async def ensure_active(self) -> tuple[str, TelegramClient]:
        """Connect the stored active phone if needed. Returns (phone, client)."""
        if self._active_client is not None:
            try:
                if self._active_client.is_connected():
                    return self._active_phone, self._active_client
            except Exception:
                pass
            self._active_client = None
        phone = database.get_active_phone(self.db_path)
        if not phone:
            raise LoginError("مفيش رقم نشط — ضيف رقم الأول.")
        await self.set_active(phone)
        assert self._active_client is not None
        return self._active_phone, self._active_client

    async def deactivate_all(self) -> None:
        async with self._lock:
            if self._active_client is not None:
                try:
                    await self._active_client.disconnect()
                except Exception:
                    pass
                self._active_client = None
            self._active_phone = ""

    @asynccontextmanager
    async def temp_client(self, phone: str) -> AsyncIterator[TelegramClient]:
        """Briefly connect any SAVED session (active or not), then disconnect.

        Used to collect dialogs from a non-active number without switching.
        If the phone is already the connected active one, reuse it.
        """
        if (self._active_client is not None and self._active_phone == phone):
            try:
                if self._active_client.is_connected():
                    yield self._active_client
                    return
            except Exception:
                pass
        acc = database.get_account(self.db_path, phone)
        if not acc:
            raise LoginError("الرقم ده مش متسجل.")
        client = self._new_client(phone)
        try:
            await self._connect(client)
            if not await client.is_user_authorized():
                raise LoginError("الجلسة انتهت — امسح الرقم وسجّله تاني.")
            yield client
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def remove(self, phone: str) -> None:
        async with self._lock:
            if self._active_phone == phone and self._active_client is not None:
                try:
                    await self._active_client.disconnect()
                except Exception:
                    pass
                self._active_client = None
                self._active_phone = ""
            database.remove_account(self.db_path, phone)
            base = str(self.session_path(phone))
            for f in (Path(base), Path(base + "-journal")):
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass
