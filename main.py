"""Main entry (Phase 2): collect links + reviewed joining via Saved Messages.

Single asyncio loop. DRY_RUN=true default: parse + save, never send.
Real sends require DRY_RUN=false AND ALLOW_LIVE_SEND=true.
Graceful shutdown: persist (already per-message), disconnect cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telethon import events  # noqa: E402

import database  # noqa: E402
import join_manager  # noqa: E402
import link_collector  # noqa: E402
from config import is_live_notifications_enabled, load_config  # noqa: E402
from link_collector import (  # noqa: E402
    build_status_text,
    extract_text_and_entities,
    format_link_notification,
    is_eligible_chat,
    process_parsed_links,
    should_ignore_for_collection,
)
from link_parser import parse_message_text, redact_url_for_logs  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from safety_manager import SafetyManager  # noqa: E402
from telegram_client import build_client, ensure_authorized, validate_account  # noqa: E402

log = logging.getLogger("userbot.main")
_stop = asyncio.Event()


_CANONICAL_CMDS = ("help", "status", "join", "pending",
                    "confirm_join", "cancel_join", "auto", "interval", "maxday",
                    "autocollect")

# Arabic aliases -> canonical command. English names keep working too.
# Every command is ONE simple word.
_CMD_ALIASES = {
    "اوامر": "help", "مساعدة": "help", "مساعده": "help",
    "ساعدني": "help", "ساعدنى": "help",
    "حالة": "status", "الحاله": "status", "الحالة": "status", "حاله": "status",
    "انضم": "join", "انضمام": "join",
    "معلقة": "pending", "المعلقة": "pending", "المعلقه": "pending",
    "معلق": "pending", "الطلبات": "pending", "طلبات": "pending",
    "أكد": "confirm_join", "اكد": "confirm_join",
    "تأكيد": "confirm_join", "تاكيد": "confirm_join",
    "الغي": "cancel_join", "الغى": "cancel_join",
    "الغاء": "cancel_join", "إلغاء": "cancel_join",
    "تلقائي": "auto", "تلقائى": "auto",
    "وقت": "interval", "الفاصل": "interval", "فاصل": "interval",
    "الوقت": "interval",
    "حد": "maxday", "الحد": "maxday",
    "اليومي": "maxday", "اليومى": "maxday",
    "جمع": "autocollect", "تجميع": "autocollect", "التجميع": "autocollect",
}


def parse_command(raw: str) -> tuple[str, str]:
    """Parse a Saved Messages command, prefix optional.

    Accepts '!tg انضم <url>', 'انضم <url>', 'join<url>', '!tg حالة', 'حالة'.
    Returns (canonical_cmd, arg). ('help', '') for explicit help/unknown
    WITH prefix; ('', '') for ordinary text (silently ignored, never replied).
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    try:
        m = re.match(r"^(?:!tg\s+)?([A-Za-z_\u0600-\u06FF]+)\s*(.*)$", text,
                     re.DOTALL)
    except Exception:
        return "", ""
    if not m:
        return "", ""
    has_prefix = text.startswith("!tg")
    word = m.group(1).lower()
    arg = (m.group(2) or "").strip()
    # tolerate join<url>, join:url, (url)
    arg = arg.strip("<> ").lstrip(":").strip().strip("<> ").strip()
    if word in _CANONICAL_CMDS:
        cmd = word
    elif word in _CMD_ALIASES:
        cmd = _CMD_ALIASES[word]
    else:
        return ("help", "") if has_prefix else ("", "")
    return cmd, arg


def _install_signals() -> None:
    def _handler(*args):
        _stop.set()
    try:
        signal.signal(signal.SIGINT, lambda s, f: _stop.set())
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, lambda s, f: _stop.set())
    except Exception:
        pass


async def run() -> int:
    try:
        cfg = load_config()
    except ValueError as e:
        print(str(e), file=sys.stderr)
        print("Fix .env (copy from .env.example). Never paste API_HASH in chat logs.", file=sys.stderr)
        return 2

    logger = setup_logging(cfg.project_root / "logs", cfg.log_level)
    _install_signals()
    database.init_db(cfg.db_path)
    safety = SafetyManager()
    # Phase 2: joins allowed ONLY via explicit !tg confirm_join (never automatic).
    safety.joining_paused = False
    live = is_live_notifications_enabled(cfg)
    logger.info(
        "Starting Phase 2 | dry_run=%s live_notify=%s collect=%s db=%s join_cooldown=%ss",
        cfg.dry_run, live, cfg.collect_enabled, cfg.db_path, cfg.join_cooldown_seconds,
    )
    if cfg.dry_run or not live:
        logger.info("DRY_RUN active: links will be saved, nothing will be sent to Telegram.")

    client = build_client(cfg)
    try:
        me = await ensure_authorized(client, cfg)
    except Exception as e:
        decision = safety.handle_error(e)
        logger.error("Auth failed (%s). Stopping safely. %s", type(e).__name__, safety.last_error)
        try:
            await client.disconnect()
        except Exception:
            pass
        return 3
    try:
        await validate_account(client)
    except Exception as e:
        logger.error("Account validation failed: %s", e)
        try:
            await client.disconnect()
        except Exception:
            pass
        return 3

    self_id: int | None = None
    try:
        self_id = getattr(me, "id", None)
    except Exception:
        pass
    # Recovery: rows left 'joining' by a crash have UNKNOWN outcome -
    # mark uncertain and tell the user to verify manually (never auto-retry).
    try:
        stale = database.recover_stale_joining(cfg.db_path)
    except Exception:
        stale = []
    if stale:
        ids = ", ".join(f"#{r['id']}" for r in stale)
        note = (f"⚠️ طلبات غير مؤكدة بعد ريستارت ({ids}): "
                f"النتيجة مجهولة — اتأكد بنفسك. "
                f"شوف: معلقة")
        logger.warning("Recovered %d stale joining request(s): %s", len(stale), ids)
        if live and safety.is_outgoing_allowed():
            try:
                await client.send_message("me", note)
            except Exception as e:
                logger.warning("Could not send recovery notice: %s", type(e).__name__)
    logger.info("Authorized. Listening for new group messages... (Ctrl+C to stop safely)")

    @client.on(events.NewMessage(chats="me"))
    async def cmd_handler(event):
        try:
            raw = (event.message.message or "") if event.message else ""
        except Exception:
            raw = ""
        cmd, arg = parse_command(raw)
        if not cmd:
            return  # ordinary saved note, not a command: stay silent
        if cmd in ("help", "--help"):
            await event.reply(
                "📖 الأوامر (من غير !tg عادي):\n"
                "اوامر — المساعدة\n"
                "حالة — التقرير\n"
                "انضم <لينكات> — معاينة + طلبات\n"
                "معلقة — الطلبات المعلقة\n"
                "أكد <رقم> — تنفيذ\n"
                "الغي <رقم> — إلغاء\n"
                "تلقائي شغل/وقف — الانضمام التلقائي\n"
                "جمع شغل/وقف — تجميع اللينكات تلقائيًا\n"
                "وقت <دقايق> — الفاصل (1-1440)\n"
                "حد <عدد> — الحد اليومي"
            )
            return
        if cmd == "status":
            try:
                stats = database.get_stats(cfg.db_path)
                pending_joins = database.count_pending_joins(cfg.db_path)
                auto = join_manager.get_auto_settings(cfg.db_path)
            except Exception as e:
                await event.reply("مش قادر أجيب الحالة — شوف اللوج.")
                return
            text = build_status_text(True, True, cfg.collect_enabled,
                                     safety.status_dict(), stats,
                                     pending_joins=pending_joins,
                                     auto_state=auto)
            await event.reply(text)
            return
        if cmd == "join":
            url = arg
            if not url:
                await event.reply("ابعت لينك: انضم https://t.me/xxx")
                return
            try:
                reply = await join_manager.handle_join_command(
                    cfg.db_path, client, safety, url)
            except Exception as e:
                safety.handle_error(e)
                reply = "الأمر فشل — شوف اللوج."
            await event.reply(reply)
            return
        if cmd == "pending":
            try:
                reply = join_manager.handle_pending(cfg.db_path)
            except Exception:
                reply = "مش قادر أعرض الطلبات."
            await event.reply(reply)
            return
        if cmd == "confirm_join":
            if not arg.isdigit():
                await event.reply("اكتب الرقم: أكد 3")
                return
            try:
                reply = await join_manager.handle_confirm(
                    cfg.db_path, client, safety, int(arg),
                    cooldown_seconds=cfg.join_cooldown_seconds, live=live)
            except Exception as e:
                safety.handle_error(e)
                reply = "التأكيد فشل — شوف اللوج."
            await event.reply(reply)
            return
        if cmd == "auto":
            word = arg.strip().lower()
            if word in ("on", "شغل", "تشغيل", "1", "yes"):
                if cfg.dry_run or not live:
                    await event.reply("وضع التجربة شغال — التلقائي مقفول.")
                    return
                join_manager.set_auto_enabled(cfg.db_path, True)
                st = join_manager.get_auto_settings(cfg.db_path)
                await event.reply(
                    f"🤖 التلقائي شغال: طلب كل {st['interval_min']} دقيقة "
                    f"(حد {st['max_per_day']}/يوم). للإيقاف: تلقائي وقف")
            elif word in ("off", "وقف", "ايقاف", "إيقاف", "0", "no"):
                join_manager.set_auto_enabled(cfg.db_path, False)
                await event.reply("التلقائي وقف.")
            else:
                st = join_manager.get_auto_settings(cfg.db_path)
                await event.reply(
                    f"التلقائي: {'شغال' if st['enabled'] else 'مقفول'} — "
                    f"كل {st['interval_min']} دقيقة — "
                    f"اليوم {st['today_count']}/{st['max_per_day']} — "
                    f"معلق {st['pending']}.")
            return
        if cmd == "interval":
            if not arg.isdigit():
                await event.reply("اكتب الدقايق: وقت 30")
                return
            ok, msg = join_manager.set_interval_minutes(cfg.db_path, int(arg))
            await event.reply(msg)
            return
        if cmd == "maxday":
            if not arg.isdigit():
                await event.reply("اكتب العدد: حد 15")
                return
            ok, msg = join_manager.set_max_per_day(cfg.db_path, int(arg))
            await event.reply(msg)
            return
        if cmd == "autocollect":
            word = arg.strip().lower()
            if word in ("on", "شغل", "تشغيل", "1", "yes"):
                if cfg.dry_run or not live:
                    await event.reply("وضع التجربة شغال — التجميع مقفول.")
                    return
                join_manager.set_autocollect(cfg.db_path, True)
                await event.reply(
                    "⚠️ التجميع شغال: أي لينك جديد هيدخل الطابور لوحده.\n"
                    "للإيقاف: جمع وقف")
            elif word in ("off", "وقف", "ايقاف", "إيقاف", "0", "no"):
                join_manager.set_autocollect(cfg.db_path, False)
                await event.reply("التجميع وقف.")
            else:
                on = join_manager.is_autocollect_enabled(cfg.db_path)
                await event.reply(f"التجميع: {'شغال' if on else 'مقفول'}.")
            return
        if cmd == "cancel_join":
            if not arg.isdigit():
                await event.reply("اكتب الرقم: الغي 3")
                return
            try:
                reply = join_manager.handle_cancel(cfg.db_path, int(arg))
            except Exception:
                reply = "الإلغاء فشل."
            await event.reply(reply)
            return
        await event.reply("مش فاهم — ابعت: اوامر")

    @client.on(events.NewMessage(incoming=True))
    async def collect_handler(event):
        if not cfg.collect_enabled:
            return
        # Commands + own messages never become sources (loop guard).
        try:
            txt0 = (event.message.message or "") if getattr(event, "message", None) else ""
            if isinstance(txt0, str) and txt0.strip().startswith("!tg"):
                return
        except Exception:
            pass
        if should_ignore_for_collection(event, self_id):
            return
        if not is_eligible_chat(event):
            return
        try:
            message = event.message
            text, entity_urls = extract_text_and_entities(message)
            parsed = parse_message_text(text, entity_urls)
            if not parsed:
                return
            for p in parsed:
                log.info("Found %s kind=%s", redact_url_for_logs(p.normalized_url), p.kind)
            # Source metadata (best effort, never crash on missing attrs)
            try:
                chat = await event.get_chat()
                title = getattr(chat, "title", None) or getattr(event, "chat_id", None)
                title = str(title) if title is not None else "Unknown"
                username = getattr(chat, "username", None)
            except Exception:
                title, username = "Unknown", None
            chat_id = str(getattr(event, "chat_id", ""))
            snippet = (text or "")[:300]
            new_recs, _upd = process_parsed_links(
                cfg.db_path, parsed, chat_id or None, title,
                str(username) if username else None, snippet,
            )
            safety.record_success(f"collected {len(parsed)} link(s)")
            try:
                database.upsert_known_chat(cfg.db_path, chat_id or "?", title, str(username) if username else None, "group")
            except Exception:
                pass
            # Opt-in autocollect (live only, user enabled): fresh links -> join queue.
            if new_recs and live and not cfg.dry_run:
                try:
                    if join_manager.is_autocollect_enabled(cfg.db_path):
                        new_norms = {r["normalized_url"] for r in new_recs}
                        fresh = [p for p in parsed if p.normalized_url in new_norms]
                        n_created, _n_skip = await join_manager.auto_enqueue_new(
                            cfg.db_path, client, safety, fresh)
                        if n_created:
                            log.info("Autocollect enqueued %d link(s).", n_created)
                except Exception as e:
                    safety.handle_error(e)
                    log.warning("Autocollect error (%s): %s",
                                type(e).__name__, safety.last_error)
            if not new_recs:
                return  # duplicate: DB updated silently (notify-once default)
            if cfg.dry_run or not live:
                log.info("DRY_RUN: would notify %d new link(s), skipped send.", len(new_recs))
                return
            if not safety.is_outgoing_allowed():
                log.warning("Outgoing paused (flood/slow/stop); links saved, notifications deferred.")
                return
            discovered = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            for rec in new_recs:
                msg = format_link_notification(rec, title, chat_id or None, discovered)
                try:
                    await client.send_message("me", msg)
                    database.mark_notified(cfg.db_path, rec["normalized_url"])
                except Exception as e:
                    decision = safety.handle_error(e)
                    log.warning("Notify failed (%s): %s", type(e).__name__, safety.last_error)
                    if decision.get("notify"):
                        try:
                            await client.send_message(
                                "me",
                                f"⚠️ Collector notice: {safety.last_error}",
                            )
                        except Exception:
                            pass
                    break
        except Exception as e:
            safety.handle_error(e)
            log.warning("Collector error (%s): %s", type(e).__name__, safety.last_error)

    async def auto_worker():
        """Background paced auto-join of the REVIEWED queue (live mode only)."""
        while not _stop.is_set():
            try:
                if live and not cfg.dry_run:
                    try:
                        report = await join_manager.process_auto_queue(
                            cfg.db_path, client, safety,
                            cooldown_seconds=cfg.join_cooldown_seconds)
                    except Exception as e:
                        safety.handle_error(e)
                        log.warning("Auto worker error (%s): %s",
                                    type(e).__name__, safety.last_error)
                        report = None
                    if report:
                        log.info("Auto step done.")
                        if safety.is_outgoing_allowed():
                            try:
                                await client.send_message("me", report)
                            except Exception as e:
                                log.warning("Auto report failed: %s", type(e).__name__)
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    continue
            except Exception as e:  # pragma: no cover - defensive
                log.warning("Auto loop error: %s", type(e).__name__)
                await asyncio.sleep(60)

    async with client:
        # run_until_disconnected replaced with stop-event wait for clean shutdown
        task = asyncio.create_task(_wait_stop())
        auto_task = asyncio.create_task(auto_worker())
        try:
            await client.run_until_disconnected()
        finally:
            task.cancel()
            auto_task.cancel()
    logger.info("Stopped cleanly. DB persisted. Never copy data/*.session to another device while running.")
    return 0


async def _wait_stop():
    await _stop.wait()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
