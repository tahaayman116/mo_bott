"""Central control bot (Bot API): inline buttons, owner-only.

Menu: انضم | تجميع | نشر | سوبر | الأرقام | حالة | إيقاف
Nobody except OWNER_ID gets any reply (silently ignored + logged).
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database
from broadcast_flow import BroadcastWorker, SuperBroadcastWorker, list_post_targets
from collect_flow import collect_dialogs, format_links_block, save_and_format
from helpers import (chunk_text, extract_links, format_duration_ar,
                     normalize_phone, parse_interval_minutes, parse_join_interval)
from join_flow import (JOIN_LIMIT_DEFAULT, JOIN_LIMIT_MAX, MANUAL_BTNS_MAX,
                       PREVIEW_LIST_MAX, JoinQueue, attempt_join,
                       clamp_join_limit, classify_link, merge_links,
                       prepare_join_items)
from link_watcher import LinkWatcher
from session_manager import LoginError
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.request import HTTPXRequest

log = logging.getLogger("centralbot")

# conversation states
ASK_PHONE, ASK_CODE, ASK_PASSWORD = range(3)
ASK_LINKS, ASK_JOIN_MODE, ASK_JOIN_IV = range(10, 13)
ASK_COLLECT_PHONE = 20
ASK_AD, ASK_POST_IV, ASK_POST_GROUPS = range(30, 33)
ASK_SUPER_ACCS, ASK_SUPER_AD, ASK_SUPER_IV, ASK_SUPER_LIMIT = range(40, 44)

OWNER = 0


def _owner_ok(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and u.id == OWNER


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ انضمام لجروبات", callback_data="m:join"),
         InlineKeyboardButton("📋 تجميع", callback_data="m:collect")],
        [InlineKeyboardButton("📢 نشر إعلان", callback_data="m:post"),
         InlineKeyboardButton("📱 الأرقام", callback_data="m:nums")],
        [InlineKeyboardButton("🚀 نشر سوبر", callback_data="m:super")],
        [InlineKeyboardButton("👁 مراقبة", callback_data="m:watch"),
         InlineKeyboardButton("📊 حالة", callback_data="m:status")],
        [InlineKeyboardButton("⏹ إيقاف الكل", callback_data="m:stop")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not _owner_ok(update):
        log.warning("Ignored non-owner user %s (owner=%s)",
                    u.id if u else "?", OWNER)
        if update.message is not None:
            try:
                await update.message.reply_text(
                    "⛔ غير مخوّل.\n"
                    f"رقمك العددي: {u.id if u else '?'}\n"
                    f"المخوّل (OWNER_ID): {OWNER}\n"
                    "إذا لم يتطابقا، عدّل OWNER_ID في .env وأعد تشغيل البوت.")
            except Exception:
                pass
        return
    await update.message.reply_text("🎛️ البوت المركزي:", reply_markup=menu_kb())


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _owner_ok(update):
        return
    data = q.data or ""
    sm = context.bot_data["sm"]
    db = context.bot_data["db"]
    if data == "m:menu":
        await q.edit_message_text("🎛️ البوت المركزي:", reply_markup=menu_kb())
    elif data == "m:status":
        await q.edit_message_text(await status_text(context),
                                  reply_markup=back_kb())
    elif data == "m:stop":
        context.bot_data["joinq"].stop()
        context.bot_data["bc"].stop()
        sbc = context.bot_data.get("superbc")
        if sbc:
            sbc.stop()
        w = context.bot_data.get("watcher")
        if w:
            await w.set_enabled(False)
            database.kv_set(db, "watch_enabled", "0")
        await q.edit_message_text("⏹ وقفت كل حاجة.", reply_markup=back_kb())
    elif data == "m:watch":
        w = context.bot_data.get("watcher")
        if w is None:
            await q.answer("المراقبة غير متاحة.", show_alert=True)
            return
        new_state = not w.is_enabled()
        started = await w.set_enabled(new_state)
        database.kv_set(db, "watch_enabled", "1" if new_state else "0")
        state_txt = "✅ شغال" if (new_state and started) else "❌ واقف"
        extra = ("\n⚠️ مفيش رقم نشط — فعّل رقم الأول." 
                 if new_state and not started else "")
        await q.edit_message_text(f"👁 مراقبة: {state_txt}{extra}\n"
                                  "يرسل لينكات جروبات جديدة (ليس قنوات).",
                                  reply_markup=menu_kb())


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 القايمة", callback_data="m:menu")]])


async def status_text(context) -> str:
    sm = context.bot_data["sm"]
    db = context.bot_data["db"]
    accs = database.list_accounts(db)
    active = database.get_active_phone(db)
    joinq: JoinQueue = context.bot_data["joinq"]
    bc: BroadcastWorker = context.bot_data["bc"]
    sbc: SuperBroadcastWorker | None = context.bot_data.get("superbc")
    w = context.bot_data.get("watcher")
    watch_state = "شغال" if (w and w.is_enabled()) else "واقف"
    if sbc and sbc.running():
        super_state = f"شغال ({len(sbc.phones)} أرقام)"
    else:
        super_state = "واقف"
    lines = ["📊 الحالة:", f"الأرقام: {len(accs)}",
             f"النشط: {active or 'مفيش'}",
             f"الانضمام التلقائي: {'شغال' if joinq.running() else 'واقف'}",
             f"النشر: {'شغال' if bc.running() else 'واقف'}",
             f"السوبر: {super_state}",
             f"المراقبة: {watch_state}",
             f"انضمامات النهاردة: {database.counter_get(db, 'joins')}",
             f"منشورات النهاردة: {database.counter_get(db, 'posts')}"]
    return "\n".join(lines)


# ---------- numbers: list / add / switch / remove ----------
async def nums_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.bot_data["db"]
    accs = database.list_accounts(db)
    active = database.get_active_phone(db)
    rows = []
    for a in accs:
        mark = "✅" if a["phone"] == active else ""
        label = f"{mark} {a['display_name'] or a['phone']} ({a['phone']})"
        rows.append([InlineKeyboardButton(label, callback_data=f"n:use:{a['phone']}")])
        rows.append([
            InlineKeyboardButton(f"🗑 {a['phone']}", callback_data=f"n:del:{a['phone']}")])
    rows.append([InlineKeyboardButton("➕ إضافة رقم", callback_data="n:add")])
    rows.append([InlineKeyboardButton("🔙 القايمة", callback_data="m:menu")])
    await q.edit_message_text("📱 الأرقام (دوس للتبديل):",
                              reply_markup=InlineKeyboardMarkup(rows))


async def _restart_watcher(context):
    w = context.bot_data.get("watcher")
    if w and w.is_enabled():
        await w.restart()


async def num_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    phone = (q.data or "").split("n:use:")[-1]
    try:
        name = await sm.set_active(phone)
    except LoginError as e:
        await q.edit_message_text(f"❌ {e}", reply_markup=back_kb())
        return
    await _restart_watcher(context)
    await q.edit_message_text(f"✅ النشط بقى: {name} ({phone})",
                              reply_markup=back_kb())


async def num_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    db = context.bot_data["db"]
    phone = (q.data or "").split("n:del:")[-1]
    await sm.remove(phone)
    await q.edit_message_text(f"🗑 اتمسح {phone}.", reply_markup=back_kb())


# add-number: fully inside Telegram (button-simple, client-proof).
# If Telegram rejects codes repeatedly, login_once.py (console) is the fallback.
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    old = context.user_data.pop("add_phone", "")
    if old:
        try:
            await sm.cancel_login(old)
        except Exception:
            pass
    context.user_data.pop("code_tries", None)
    await q.edit_message_text("📱 ابعت الرقم (+كود البلد):\nمثال: +201012345678",
                              reply_markup=cancel_kb())
    return ASK_PHONE


async def add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = normalize_phone(update.message.text or "")
    if not phone:
        await update.message.reply_text("الرقم غلط — ابعته كده: +201012345678")
        return ASK_PHONE
    sm = context.bot_data["sm"]
    try:
        await sm.start_login(phone)
    except LoginError as e:
        await update.message.reply_text(f"❌ {e}", reply_markup=menu_kb())
        return ConversationHandler.END
    context.user_data["add_phone"] = phone
    context.user_data["code_tries"] = 0
    await update.message.reply_text(f"📩 اتبعت كود لـ {phone} — ابعته هنا فورًا (أرقام بس).",
                                    reply_markup=cancel_kb())
    return ASK_CODE


async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sm = context.bot_data["sm"]
    phone = context.user_data.get("add_phone", "")
    try:
        res = await sm.submit_code(phone, update.message.text or "")
    except LoginError as e:
        tries = context.user_data.get("code_tries", 0) + 1
        context.user_data["code_tries"] = tries
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 ابعت كود جديد", callback_data="n:resend")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
        ])
        hint = ("\nلو فشل تاني: ضيف الرقم من الكمبيوتر بأمر واحد "
                "(اسألني وأقولك).") if tries >= 2 else ""
        await update.message.reply_text(
            f"❌ {e}\nاستخدم **آخر كود يوصلك فورًا** — القديم بيبطل لوحده.{hint}",
            reply_markup=kb)
        return ASK_CODE
    if res == "need_password":
        await update.message.reply_text("🔑 الحساب عليه تحقق بخطوتين — ابعت الباسورد.",
                                        reply_markup=cancel_kb())
        return ASK_PASSWORD
    context.user_data.pop("add_phone", None)
    context.user_data.pop("code_tries", None)
    try:
        await sm.set_active(phone)
    except LoginError:
        pass
    await _restart_watcher(context)
    await update.message.reply_text(f"✅ اتسجل وبقى نشط: {phone}", reply_markup=menu_kb())
    return ConversationHandler.END


async def add_resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    phone = context.user_data.get("add_phone", "")
    if not phone:
        await q.edit_message_text("ابدأ من الأول.", reply_markup=menu_kb())
        return ConversationHandler.END
    try:
        await sm.start_login(phone)
    except LoginError as e:
        await q.edit_message_text(f"❌ {e}", reply_markup=menu_kb())
        return ConversationHandler.END
    await q.edit_message_text(
        f"📩 اتبعت كود جديد لـ {phone} — ابعته هنا بسرعة قبل ما ينتهي.",
        reply_markup=cancel_kb())
    return ASK_CODE


async def add_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sm = context.bot_data["sm"]
    phone = context.user_data.get("add_phone", "")
    try:
        await sm.submit_password(phone, update.message.text or "")
    except LoginError as e:
        await update.message.reply_text(f"❌ {e}")
        return ASK_PASSWORD
    try:
        await update.message.delete()
    except Exception:
        pass
    context.user_data.pop("add_phone", None)
    context.user_data.pop("code_tries", None)
    try:
        await sm.set_active(phone)
    except LoginError:
        pass
    await _restart_watcher(context)
    await context.bot.send_message(update.effective_chat.id,
                                   f"✅ اتسجل وبقى نشط: {phone}",
                                   reply_markup=menu_kb())
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        sm = context.bot_data["sm"]
        phone = context.user_data.pop("add_phone", "")
        if phone:
            await sm.cancel_login(phone)
        await q.edit_message_text("اتلغى.", reply_markup=menu_kb())
    elif update.message:
        await update.message.reply_text("اتلغى.", reply_markup=menu_kb())
    context.user_data.clear()
    return ConversationHandler.END


async def reset_flow_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner_ok(update):
        return
    sm = context.bot_data.get("sm")
    phone = context.user_data.pop("add_phone", "")
    if phone and sm is not None:
        try:
            await sm.cancel_login(phone)
        except Exception:
            pass
    for key in ("code_tries", "join_links_all", "collect_phone", "ad_text",
                "post_iv", "post_phone", "post_targets", "post_sel",
                "post_page", "super_accs", "super_text", "super_iv",
                "super_limit"):
        context.user_data.pop(key, None)
    for conv in context.bot_data.get("convs", ()):
        try:
            key = conv._get_key(update)
            states = getattr(conv, "_conversations", None)
            if isinstance(states, dict):
                states.pop(key, None)
        except Exception:
            pass


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")]])


def interval_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10 ثواني", callback_data=f"{prefix}s:10"),
         InlineKeyboardButton("20 ثانية", callback_data=f"{prefix}s:20"),
         InlineKeyboardButton("30 ثانية", callback_data=f"{prefix}s:30")],
        [InlineKeyboardButton("1 دقيقة", callback_data=f"{prefix}:1"),
         InlineKeyboardButton("5 دقايق", callback_data=f"{prefix}:5")],
        [InlineKeyboardButton("10 دقايق", callback_data=f"{prefix}:10"),
         InlineKeyboardButton("30 دقيقة", callback_data=f"{prefix}:30")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
    ])


# ---------- join flow ----------
def get_join_limit(db) -> int:
    try:
        return clamp_join_limit(int(database.kv_get(db, "join_limit",
                                                    str(JOIN_LIMIT_DEFAULT))))
    except ValueError:
        return JOIN_LIMIT_DEFAULT


async def join_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("join_links_all", None)
    context.user_data.pop("join_items", None)
    limit = get_join_limit(context.bot_data["db"])
    await q.edit_message_text(
        f"🔗 ابعت اللينكات على رسالة أو أكتر، ولما تخلص دوس تم (الحد: {limit}):\n"
        f"ابعت رقم 1-{JOIN_LIMIT_MAX} لوحده عشان تغير الحد.",
        reply_markup=cancel_kb())
    return ASK_LINKS


def build_join_preview(items, folders):
    """Returns (text, keyboard). Long lists are summarized, never cut off."""
    lines = [f"🔍 لقيت {len(items)} لينك:"]
    for i, (url, kind, _) in enumerate(items[:PREVIEW_LIST_MAX], 1):
        lines.append(f"{i}. {url}")
    if len(items) > PREVIEW_LIST_MAX:
        lines.append(f"... و {len(items) - PREVIEW_LIST_MAX} كمان")
    if folders:
        lines.append(f"({folders} فولدر — سيضم كل جروباته)")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ تلقائي بوقت", callback_data="j:auto"),
         InlineKeyboardButton("✋ يدوي", callback_data="j:manual")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
    ])
    return "\n".join(lines), kb


def build_manual_rows(items):
    rows = []
    for i, (url, _, _) in enumerate(items[:MANUAL_BTNS_MAX]):
        rows.append([InlineKeyboardButton(f"➕ انضم: {url[:40]}",
                                          callback_data=f"j:one:{i}")])
    if len(items) > MANUAL_BTNS_MAX:
        rows.append([InlineKeyboardButton(
            f"... و {len(items) - MANUAL_BTNS_MAX} كمان (تأكيد الكل يشملهم)",
            callback_data="j:noop")])
    rows.append([InlineKeyboardButton("✅ تأكيد الكل", callback_data="j:all")])
    rows.append([InlineKeyboardButton("🔙 القايمة", callback_data="m:menu")])
    return rows


async def join_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    text = update.message.text or ""
    links = extract_links(text)
    if not links:
        # lone number = change the limit freely
        n = parse_interval_minutes(text)
        if n is not None and 1 <= n <= JOIN_LIMIT_MAX:
            database.kv_set(db, "join_limit", str(n))
            await update.message.reply_text(
                f"✅ حد اللينكات بقى {n} — ابعت اللينكات دلوقتي.")
        else:
            await update.message.reply_text(
                "مفيش لينكات — ابعت لينكات t.me (أو رقم لتغيير الحد).")
        return ASK_LINKS
    acc = merge_links(context.user_data.get("join_links_all", []), links)
    context.user_data["join_links_all"] = acc
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ تم ({len(acc)} لينك)", callback_data="j:done")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
    ])
    await update.message.reply_text(
        f"اتجمع {len(acc)} لينك — ابعت كمان أو دوس تم.", reply_markup=kb)
    return ASK_LINKS


async def join_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.bot_data["db"]
    links = context.user_data.get("join_links_all", [])
    if not links:
        await q.answer("مفيش لينكات لسه — ابعت لينكات الأول.", show_alert=True)
        return ASK_LINKS
    limit = get_join_limit(db)
    if len(links) > limit:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⬆️ خلي الحد {min(len(links), JOIN_LIMIT_MAX)} وكمل",
                                  callback_data=f"j:lim:{min(len(links), JOIN_LIMIT_MAX)}")],
            [InlineKeyboardButton(f"كمل بأول {limit} بس", callback_data="j:limkeep")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
        ])
        await q.edit_message_text(
            f"جمعت {len(links)} لينك والحد الحالي {limit} — تحب إيه؟",
            reply_markup=kb)
        return ASK_LINKS
    items, folders = prepare_join_items(links)
    context.user_data.pop("join_links_all", None)
    if not items:
        await q.edit_message_text("مفيش لينكات صالحة (الفولدرات بتتفتح يدوي).",
                                  reply_markup=menu_kb())
        return ConversationHandler.END
    context.user_data["join_items"] = items
    text, kb = build_join_preview(items, folders)
    await q.edit_message_text(text, reply_markup=kb)
    return ASK_JOIN_MODE


async def join_lim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.bot_data["db"]
    data = q.data or ""
    links = context.user_data.pop("join_links_all", [])
    if data.startswith("j:lim:"):
        try:
            n = clamp_join_limit(int(data.split("j:lim:")[-1]))
        except ValueError:
            n = get_join_limit(db)
        database.kv_set(db, "join_limit", str(n))
        await q.edit_message_text(f"✅ الحد بقى {n} — دي اللينكات:")
    else:  # j:limkeep
        n = get_join_limit(db)
        links = links[:n]
        await q.edit_message_text("تمام — هكمل بالعدد المحدود:")
    items, folders = prepare_join_items(links)
    if not items:
        await q.edit_message_text("مفيش لينكات صالحة.", reply_markup=menu_kb())
        return ConversationHandler.END
    context.user_data["join_items"] = items
    text, kb = build_join_preview(items, folders)
    await q.edit_message_text(text, reply_markup=kb)
    return ASK_JOIN_MODE


async def join_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "j:manual":
        items = context.user_data.get("join_items", [])
        await q.edit_message_text(f"دوس على كل لينك عشان تنضم ({len(items)}):",
                                  reply_markup=InlineKeyboardMarkup(
                                      build_manual_rows(items)))
        # NOTE: keep join_items in user_data on purpose — j:one / j:all
        # use them after this conversation ends.
        return ConversationHandler.END
    await q.edit_message_text(
        "⏱️ الوقت بين كل انضمام والتاني؟\n"
        "(ابعت رقم مخصص بالدقايق، أو رقم + s للثواني — أقل 10 ثواني)",
        reply_markup=interval_kb("j:iv"))
    return ASK_JOIN_IV


async def join_iv_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("j:ivs:"):
        seconds = int(data.rsplit(":", 1)[-1])
    else:
        seconds = int(data.rsplit(":", 1)[-1]) * 60
    await start_join_queue(update, context, seconds, edit=True)
    return ConversationHandler.END


async def join_iv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seconds = parse_join_interval(update.message.text or "")
    if not seconds:
        await update.message.reply_text(
            "ابعت رقم من 10 ثواني لـ 24 ساعة.\n"
            "مثال: 30s أو 10 ثواني أو 5 (دقايق).")
        return ASK_JOIN_IV
    await start_join_queue(update, context, seconds, edit=False)
    return ConversationHandler.END


async def start_join_queue(update, context, seconds: int, edit: bool):
    sm = context.bot_data["sm"]
    joinq: JoinQueue = context.bot_data["joinq"]
    items = context.user_data.get("join_items", [])
    context.user_data.clear()
    try:
        phone, _ = await sm.ensure_active()
    except LoginError as e:
        txt = f"❌ {e}"
        if edit:
            await update.callback_query.edit_message_text(txt, reply_markup=menu_kb())
        else:
            await update.message.reply_text(txt, reply_markup=menu_kb())
        return
    pairs = [(k, p) for _, k, p in items]
    joinq.start(phone, pairs, seconds)
    txt = (f"⚡ التلقائي شغال: {len(pairs)} لينك كل "
           f"{format_duration_ar(joinq.interval_sec)}.")
    if edit:
        await update.callback_query.edit_message_text(txt, reply_markup=back_kb())
    else:
        await update.message.reply_text(txt, reply_markup=menu_kb())


async def join_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("دول أول 40 — تأكيد الكل يشمل الليستة كلها.", show_alert=True)


async def join_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    idx = int((q.data or "").split("j:one:")[-1])
    items = context.user_data.get("join_items", [])
    if idx >= len(items):
        await q.edit_message_text("خلصت.", reply_markup=menu_kb())
        return
    url, kind, payload = items[idx]
    manual: list[str] = []
    try:
        _, client = await sm.ensure_active()
        res, manual = await attempt_join(client, kind, payload)
    except Exception as e:
        res = f"مش قادر أوصل ({type(e).__name__})."
    database.counter_inc(context.bot_data["db"], "joins")
    await q.answer("✅ انضميت" if res == "done" else res, show_alert=False)
    shown = "📁 فولدر" if kind == "folder" else url
    text = f"{shown}\nالنتيجة: {'✅ انضميت' if res == 'done' else res}"
    if manual:
        text += "\n📋 جروبات تحتاج انضمام يدوي:\n" + "\n".join(manual)
    chunks = chunk_text(text, 4000)
    await q.edit_message_text(chunks[0], reply_markup=back_kb())
    for extra in chunks[1:]:
        await send_bg(context.bot, update.effective_chat.id, extra, back_kb())


async def join_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = context.user_data.get("join_items", [])
    context.user_data.pop("join_items", None)
    if not items:
        await q.edit_message_text("ماشي — راجع القايمة.", reply_markup=back_kb())
        return
    await q.edit_message_text(
        f"⏳ جارى الانضمام لـ {len(items)} لينك… هبعتلك النتيجة لما يخلص.")
    asyncio.create_task(join_all_job(context.bot_data, context.bot,
                                     update.effective_chat.id, items))


async def join_all_job(bot_data: dict, bot, chat_id: int,
                       items: list) -> None:
    try:
        _, client = await bot_data["sm"].ensure_active()
    except LoginError as e:
        await send_bg(bot, chat_id, f"❌ {e}", back_kb())
        return
    ok = 0
    n = len(items)
    last = {"t": 0.0}
    manual_all: list[str] = []
    for idx, (_, kind, payload) in enumerate(items, 1):
        try:
            res, manual = await attempt_join(client, kind, payload)
            if "انضميت" in res or "عضو أصلًا" in res or res == "done":
                ok += 1
            manual_all.extend(manual)
        except Exception:
            pass
        database.counter_inc(bot_data["db"], "joins")
        now = time.monotonic()
        if idx < n and now - last["t"] >= 10:
            last["t"] = now
            await send_bg(bot, chat_id, f"⏳ {idx}/{n} خلصو ({ok} ✅)…")
    await send_bg(bot, chat_id, f"🏁 {ok}✅ من {n}.", back_kb())
    if manual_all:
        body = "📋 جروبات تحتاج انضمام يدوي:\n" + "\n".join(manual_all)
        for chunk in chunk_text(body, 4000):
            await send_bg(bot, chat_id, chunk, back_kb())


# ---------- collect flow ----------
async def collect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.bot_data["db"]
    active = database.get_active_phone(db)
    await q.edit_message_text(
        f"📋 ابعت الرقم اللي عايز تجمع منه\n(النشط حاليًا: {active or 'مفيش'} — ممكن تبعته هو):",
        reply_markup=cancel_kb())
    return ASK_COLLECT_PHONE


async def collect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = normalize_phone(update.message.text or "")
    if not phone:
        await update.message.reply_text("الرقم غلط — مثال: +201012345678")
        return ASK_COLLECT_PHONE
    chat_id = update.effective_chat.id
    asyncio.create_task(collect_job(context.bot_data, context.bot, chat_id, phone))
    await update.message.reply_text(
        "🚀 شغلنا التجميع في الخلفية — الأزرار مش هتقعد تدور، والنتيجة هتجي برسايل.")
    return ConversationHandler.END


async def collect_job(bot_data: dict, bot, chat_id: int, phone: str) -> None:
    """Heavy work OFF the update loop, with progress + hard timeout."""
    sm = bot_data["sm"]
    db = bot_data["db"]
    status = None
    try:
        status = await bot.send_message(chat_id, f"🔄 بجمع من {phone}…")
    except Exception:
        status = None
    last = {"t": 0.0}

    async def prog(n: int) -> None:
        now = time.monotonic()
        if not status or now - last["t"] < 4:
            return
        last["t"] = now
        try:
            await status.edit_text(f"🔄 بجمع من {phone}… {n} شات.")
        except Exception:
            pass

    try:
        async with sm.temp_client(phone) as client:
            items = await asyncio.wait_for(collect_dialogs(client, prog),
                                           timeout=600)
    except LoginError as e:
        await send_bg(bot, chat_id, f"❌ {e}", menu_kb())
        return
    except (asyncio.TimeoutError, TimeoutError):
        await send_bg(bot, chat_id,
                      "❌ جمع بطيء مقفش بسبب النت — ابعت الرقم تاني.",
                      menu_kb())
        return
    except Exception as e:
        await send_bg(bot, chat_id,
                      f"❌ فشل ({type(e).__name__}) — جرب تاني أو غير النت.",
                      menu_kb())
        return
    if status:
        try:
            await status.edit_text("✅ خلص الجمع.")
        except Exception:
            pass
    for chunk in save_and_format(db, phone, items):
        await send_bg(bot, chat_id, chunk, None)
    blocks = format_links_block(items)
    if blocks:
        n_links = sum(1 for i in items if i.get("link"))
        await send_bg(bot, chat_id,
                      f"📋 {n_links} رابط جاهز — دوس على المربع وانسخ الكل:", None)
        for b in blocks:
            await send_bg(bot, chat_id, b, None, parse_mode="Markdown")
    await send_bg(bot, chat_id, "خلص ✅ — الزق اللي نسخته في ➕ انضم.",
                  menu_kb())


async def send_bg(bot, chat_id: int, text: str, kb=None, parse_mode=None) -> None:
    """Fire-and-forget safety net for results sent from background tasks."""
    try:
        await bot.send_message(chat_id, text, reply_markup=kb,
                               parse_mode=parse_mode)
    except Exception:
        log.warning("bg send to %s failed", chat_id)


# ---------- broadcast flow ----------
async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    for key in ("ad_text", "post_iv", "post_phone", "post_targets",
                "post_sel", "post_page"):
        context.user_data.pop(key, None)
    await q.edit_message_text("📢 ابعت نص الإعلان (نص بس):", reply_markup=cancel_kb())
    return ASK_AD


async def post_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text) < 5:
        await update.message.reply_text(
            "الإعلان قصير أوي — ابعت نص الإعلان الكامل (مش كلمة للتجربة).")
        return ASK_AD
    context.user_data["ad_text"] = text[:3000]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("5 دقايق", callback_data="p:iv:5"),
         InlineKeyboardButton("10 دقايق", callback_data="p:iv:10")],
        [InlineKeyboardButton("15 دقيقة", callback_data="p:iv:15"),
         InlineKeyboardButton("30 دقيقة", callback_data="p:iv:30")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")],
    ])
    preview = text if len(text) <= 200 else text[:200] + "…"
    await update.message.reply_text(
        f"📢 إعلانك:\n{preview}\n\n⏱️ ينشر كل كام دقيقة؟\n(مثال: 5 = منشور كل 5 دقايق — أو ابعت رقم مخصص)",
        reply_markup=kb)
    return ASK_POST_IV


async def post_iv_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    minutes = int((q.data or "").split("p:iv:")[-1])
    await show_group_select(update, context, minutes, edit=True)
    return ASK_POST_GROUPS


async def post_iv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    minutes = parse_interval_minutes(update.message.text or "")
    if not minutes:
        await update.message.reply_text("ابعت رقم من 1 لـ 1440.")
        return ASK_POST_IV
    await show_group_select(update, context, minutes, edit=False)
    return ASK_POST_GROUPS


POST_PAGE_SIZE = 20


def groups_kb(targets: list, sel: set, page: int = 0) -> InlineKeyboardMarkup:
    total = len(targets)
    start = page * POST_PAGE_SIZE
    end = min(total, start + POST_PAGE_SIZE)
    rows = []
    for i in range(start, end):
        ent, can_post = targets[i]
        mark = "✅" if i in sel else "⬜"
        lock = "" if can_post else "🔇"
        title = (getattr(ent, "title", "") or "?")[:28]
        rows.append([InlineKeyboardButton(f"{mark}{lock} {title}",
                                          callback_data=f"p:t:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"p:page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{(total + POST_PAGE_SIZE - 1)//POST_PAGE_SIZE}",
                                    callback_data="p:noop"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"p:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✅ الكل", callback_data="p:all"),
                 InlineKeyboardButton("⬜ مسح", callback_data="p:none")])
    rows.append([InlineKeyboardButton("▶️ ابدأ النشر", callback_data="p:start"),
                 InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")])
    return InlineKeyboardMarkup(rows)


def groups_text(targets: list, sel: set, minutes: int, page: int = 0) -> str:
    n_no_post = sum(1 for _, c in targets if not c)
    extra = f"\n🔇 = قناة مش أدمن فيها (هتتخطى)." if n_no_post else ""
    total_pages = (len(targets) + POST_PAGE_SIZE - 1) // POST_PAGE_SIZE
    pg = f" (صفحة {page+1}/{total_pages})" if total_pages > 1 else ""
    return (f"📢 اختار الشاتات ({len(sel)} من {len(targets)}) — كل {minutes} دقيقة{pg}:\n"
            f"دوس على الشات عشان تشيل/ترجع علامته.{extra}")


async def show_group_select(update, context, minutes: int, edit: bool):
    context.user_data["post_iv"] = minutes
    if edit:
        await update.callback_query.edit_message_text("🔄 بجيب الشاتات…")
    else:
        await update.message.reply_text(
            "🔄 بجيب الشاتات… (لو الحساب فيه كتير هياخد شوية)")
    asyncio.create_task(group_select_job(context, update, minutes, edit))


async def group_select_job(context: ContextTypes.DEFAULT_TYPE, update,
                           minutes: int, edit: bool) -> None:
    sm = context.bot_data["sm"]
    try:
        phone, client = await asyncio.wait_for(sm.ensure_active(), timeout=60)
        targets = await asyncio.wait_for(list_post_targets(client),
                                         timeout=300)
    except LoginError as e:
        txt = f"❌ {e}"
        context.user_data.clear()
        await _edit_or_send(context, update, edit, txt, menu_kb())
        return
    except (asyncio.TimeoutError, TimeoutError):
        context.user_data.clear()
        await _edit_or_send(context, update, edit,
                            "❌ الاتصال بالحساب اتأخر — لو فيه تسجيل رقم شغال "
                            "استنى شوية وجرب تاني.",
                            menu_kb())
        return
    except Exception as e:
        context.user_data.clear()
        await _edit_or_send(context, update, edit,
                            f"❌ فشل ({type(e).__name__}) — جرب تاني أو غير النت.",
                            menu_kb())
        return
    if not targets:
        context.user_data.clear()
        await _edit_or_send(context, update, edit, "مفيش جروبات في الحساب ده.",
                            menu_kb())
        return
    context.user_data["post_phone"] = phone
    context.user_data["post_targets"] = targets
    context.user_data["post_sel"] = set(range(len(targets)))  # الكل متعلم
    context.user_data["post_page"] = 0
    kb = groups_kb(targets, context.user_data["post_sel"], 0)
    txt = groups_text(targets, context.user_data["post_sel"], minutes, 0)
    await _edit_or_send(context, update, edit, txt, kb)


async def _edit_or_send(context, update, edit: bool, text: str, kb) -> None:
    try:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            chat = update.effective_chat
            if chat is None:
                return
            await context.bot.send_message(chat.id, text, reply_markup=kb)
    except Exception:
        pass


async def post_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    targets = context.user_data.get("post_targets", [])
    sel: set = context.user_data.get("post_sel", set())
    page = context.user_data.get("post_page", 0)
    data = q.data or ""
    if data == "p:all":
        sel = set(range(len(targets)))
    elif data == "p:none":
        sel = set()
    elif data.startswith("p:t:"):
        try:
            i = int(data.split("p:t:")[-1])
        except ValueError:
            i = -1
        if 0 <= i < len(targets):
            sel.discard(i) if i in sel else sel.add(i)
    elif data.startswith("p:page:"):
        try:
            page = int(data.split("p:page:")[-1])
        except ValueError:
            page = 0
        context.user_data["post_page"] = page
    elif data == "p:noop":
        return ASK_POST_GROUPS
    context.user_data["post_sel"] = sel
    minutes = context.user_data.get("post_iv", 5)
    await q.edit_message_text(groups_text(targets, sel, minutes, page),
                              reply_markup=groups_kb(targets, sel, page))
    return ASK_POST_GROUPS


async def post_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sm = context.bot_data["sm"]
    bc: BroadcastWorker = context.bot_data["bc"]
    sbc: SuperBroadcastWorker | None = context.bot_data.get("superbc")
    if sbc and sbc.running():
        context.user_data.clear()
        await q.edit_message_text(
            "⚠️ السوبر شغال — أوقفه الأول قبل النشر العادي.",
            reply_markup=menu_kb())
        return ConversationHandler.END
    text = context.user_data.get("ad_text", "")
    minutes = context.user_data.get("post_iv", 5)
    phone = context.user_data.get("post_phone", "")
    saved_targets = context.user_data.get("post_targets", [])
    sel: set = context.user_data.get("post_sel", set())
    if not text:
        context.user_data.clear()
        await q.edit_message_text("اتلغى.", reply_markup=menu_kb())
        return ConversationHandler.END
    if saved_targets and not sel:
        await q.answer("اختار جروب واحد على الأقل.", show_alert=True)
        return ASK_POST_GROUPS
    context.user_data.clear()
    try:
        if saved_targets and sel:
            targets = [saved_targets[i] for i in sorted(sel)]
        else:
            async def _fetch_targets():
                async with sm.temp_client(phone) as client:
                    return await list_post_targets(client)

            targets = await asyncio.wait_for(_fetch_targets(), timeout=300)
    except LoginError as e:
        await q.edit_message_text(f"❌ {e}", reply_markup=menu_kb())
        return ConversationHandler.END
    except (asyncio.TimeoutError, TimeoutError):
        await q.edit_message_text("❌ النت بطيء — جرب تاني.",
                                  reply_markup=menu_kb())
        return ConversationHandler.END
    if not targets:
        await q.edit_message_text("مفيش جروبات.", reply_markup=menu_kb())
        return ConversationHandler.END
    bid = bc.start(phone, text, minutes, targets)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ إيقاف النشر", callback_data="p:stop")],
        [InlineKeyboardButton("🔙 القايمة", callback_data="m:menu")],
    ])
    await q.edit_message_text(f"📢 شغال (#{bid}): {len(targets)} شات كل {minutes} دقيقة.",
                              reply_markup=kb)
    return ConversationHandler.END


async def post_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.bot_data["bc"].stop()
    await q.edit_message_text("⏹ وقفت النشر.", reply_markup=back_kb())


# ---------- super broadcast flow ----------
def super_accs_text(accs: list, sel: set) -> str:
    return (f"🚀 السوبر — اختار الأرقام ({len(sel)} من {len(accs)}):\n"
            "هينشر في كل الشاتات القابلة للنشر لكل رقم مختار.")


def super_accs_kb(accs: list, sel: set) -> InlineKeyboardMarkup:
    rows = []
    for a in accs:
        mark = "✅" if a["phone"] in sel else "⬜"
        label = (a["display_name"] or a["phone"])[:40]
        rows.append([InlineKeyboardButton(
            f"{mark} {label}", callback_data=f"s:acc:{a['phone']}")])
    rows.append([InlineKeyboardButton("✅ الكل", callback_data="s:all"),
                 InlineKeyboardButton("⬜ مسح", callback_data="s:none")])
    rows.append([InlineKeyboardButton("➡️ متابعة", callback_data="s:next"),
                 InlineKeyboardButton("❌ إلغاء", callback_data="x:cancel")])
    return InlineKeyboardMarkup(rows)


async def super_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _owner_ok(update):
        return ConversationHandler.END
    for key in ("super_accs", "super_text", "super_iv", "super_limit"):
        context.user_data.pop(key, None)
    sbc: SuperBroadcastWorker = context.bot_data["superbc"]
    if sbc.running():
        await q.edit_message_text(
            "⚠️ السوبر شغال بالفعل — أوقفه الأول من ⏹ إيقاف الكل.",
            reply_markup=menu_kb())
        return ConversationHandler.END
    accs = database.list_accounts(context.bot_data["db"])
    if not accs:
        await q.edit_message_text("ضيف رقم أولًا من 📱 الأرقام.",
                                  reply_markup=menu_kb())
        return ConversationHandler.END
    sel = {a["phone"] for a in accs}
    context.user_data["super_accs"] = sel
    try:
        await q.edit_message_text(super_accs_text(accs, sel),
                                  reply_markup=super_accs_kb(accs, sel))
    except Exception:
        pass
    return ASK_SUPER_ACCS


async def super_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _owner_ok(update):
        return
    db = context.bot_data["db"]
    accs = database.list_accounts(db)
    valid = {a["phone"] for a in accs}
    sel = context.user_data.get("super_accs")
    if not isinstance(sel, set):
        sel = set(valid)
    sel &= valid
    data = q.data or ""
    if data == "s:all":
        sel = set(valid)
    elif data == "s:none":
        sel = set()
    elif data.startswith("s:acc:"):
        phone = data.split("s:acc:", 1)[-1]
        if phone in valid:
            if phone in sel:
                sel.discard(phone)
            else:
                sel.add(phone)
    context.user_data["super_accs"] = sel
    try:
        await q.edit_message_text(super_accs_text(accs, sel),
                                  reply_markup=super_accs_kb(accs, sel))
    except Exception:
        pass
    return ASK_SUPER_ACCS


async def super_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _owner_ok(update):
        await q.answer()
        return
    sel = context.user_data.get("super_accs", set())
    if not sel:
        await q.answer("اختار رقم واحد على الأقل.", show_alert=True)
        return ASK_SUPER_ACCS
    await q.answer()
    context.user_data.pop("super_text", None)
    context.user_data.pop("super_iv", None)
    await q.edit_message_text(
        "🚀 ابعت نص الإعلان اللي هينشر من الأرقام المختارة:",
        reply_markup=cancel_kb())
    return ASK_SUPER_AD


async def super_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner_ok(update):
        return
    text = (update.message.text or "").strip()
    if len(text) < 5:
        await update.message.reply_text(
            "الإعلان قصير أوي — ابعت نص الإعلان الكامل.")
        return ASK_SUPER_AD
    context.user_data["super_text"] = text[:3000]
    preview = text if len(text) <= 200 else text[:200] + "…"
    await update.message.reply_text(
        f"📢 إعلان السوبر:\n{preview}\n\n"
        "⏱️ ينشر كل كام؟ (ثواني أو دقايق — أقل 10 ثواني)",
        reply_markup=interval_kb("s:iv"))
    return ASK_SUPER_IV


async def super_iv_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _owner_ok(update):
        return
    data = q.data or ""
    if data.startswith("s:ivs:"):
        seconds = int(data.rsplit(":", 1)[-1])
    else:
        seconds = int(data.rsplit(":", 1)[-1]) * 60
    context.user_data["super_iv"] = seconds
    await q.edit_message_text(
        f"⏱️ كل {format_duration_ar(seconds)}.\n"
        "اكتب الحد اليومي لكل رقم (عدد المنشورات) — 0 = بدون حد:",
        reply_markup=cancel_kb())
    return ASK_SUPER_LIMIT


async def super_iv_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner_ok(update):
        return
    seconds = parse_join_interval(update.message.text or "")
    if not seconds:
        await update.message.reply_text(
            "ابعت رقم من 10 ثواني لـ 24 ساعة.\n"
            "مثال: 30s أو 10 ثواني أو 5 (دقايق).")
        return ASK_SUPER_IV
    context.user_data["super_iv"] = seconds
    await update.message.reply_text(
        f"⏱️ كل {format_duration_ar(seconds)}.\n"
        "اكتب الحد اليومي لكل رقم (عدد المنشورات) — 0 = بدون حد:",
        reply_markup=cancel_kb())
    return ASK_SUPER_LIMIT


async def super_limit_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner_ok(update):
        return
    raw = (update.message.text or "").strip()
    if not raw.isdigit() or int(raw) > 100000:
        await update.message.reply_text(
            "اكتب رقم صحيح من 0 لـ 100000 (0 = بدون حد).")
        return ASK_SUPER_LIMIT
    limit = int(raw)
    phones = sorted(context.user_data.get("super_accs", set()))
    text = context.user_data.get("super_text", "")
    seconds = context.user_data.get("super_iv", 60)
    context.user_data.clear()
    if not phones or not text:
        await update.message.reply_text("ابدأ من الأول.", reply_markup=menu_kb())
        return ConversationHandler.END
    sbc: SuperBroadcastWorker = context.bot_data["superbc"]
    if sbc.running():
        await update.message.reply_text(
            "السوبر شغال بالفعل — أوقفه الأول.", reply_markup=menu_kb())
        return ConversationHandler.END
    bc: BroadcastWorker = context.bot_data["bc"]
    had_normal = bc.running()
    bc.stop()
    sbc.start(phones, text, seconds, limit)
    limit_txt = f"{sbc.daily_limit} لكل رقم" if sbc.daily_limit else "بدون حد"
    extra = "\n⏹ وقفت النشر العادي عشان مفيش تكرار." if had_normal else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ إيقاف السوبر", callback_data="s:stop")],
        [InlineKeyboardButton("🔙 القايمة", callback_data="m:menu")],
    ])
    await update.message.reply_text(
        f"🚀 السوبر شغال: {len(phones)} رقم — كل "
        f"{format_duration_ar(sbc.interval_sec)} — الحد اليومي: {limit_txt}.{extra}",
        reply_markup=kb)
    return ConversationHandler.END


async def super_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _owner_ok(update):
        return
    sbc = context.bot_data.get("superbc")
    if sbc:
        sbc.stop()
    try:
        await q.edit_message_text("⏹ وقفت السوبر.", reply_markup=back_kb())
    except Exception:
        pass


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Network blips -> tell the owner to resend; duplicates -> warn loudly."""
    err = context.error
    log.warning("handler error: %r", err)
    if isinstance(err, Conflict):
        msg = ("⚠️ فيه نسخة تانية من البوت شغالة! اقفل أي نافذة زيادة "
               "وخلي واحدة بس.")
    elif isinstance(err, (TimedOut, NetworkError)):
        msg = "⚠️ النت قطع لحظة وموصلتش — ابعت آخر حاجة تاني."
    else:
        msg = f"❌ خطأ: {type(err).__name__}: {str(err)[:200]}"
    try:
        if update is not None and update.effective_chat is not None \
                and _owner_ok(update):
            await update.effective_chat.send_message(msg)
        elif OWNER:
            await context.bot.send_message(OWNER, msg)
    except Exception:
        pass


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if not _owner_ok(update):
        return
    await reset_flow_state(update, context)
    text = "⌛ الزرار ده انتهى — ابدأ من القايمة:"
    try:
        await q.edit_message_text(text, reply_markup=menu_kb())
    except Exception:
        try:
            await context.bot.send_message(update.effective_chat.id, text,
                                           reply_markup=menu_kb())
        except Exception:
            pass


# ---------- app wiring ----------
def build_app(cfg, sm) -> Application:
    global OWNER
    OWNER = cfg.owner_id
    database.init_db(cfg.db_path)
    database.kv_set(cfg.db_path, "post_cap", str(cfg.post_daily_cap))

    app = (Application.builder().token(cfg.bot_token)
           .request(HTTPXRequest(connection_pool_size=8,
                                 connect_timeout=20.0,
                                 read_timeout=30.0,
                                 write_timeout=30.0,
                                 pool_timeout=10.0))
           .build())
    app.bot_data["sm"] = sm
    app.bot_data["db"] = cfg.db_path

    async def _notify(text: str):
        try:
            await app.bot.send_message(cfg.owner_id, text)
        except Exception as e:
            log.warning("notify failed: %s", e)

    async def _get_client():
        return await sm.ensure_active()

    app.bot_data["joinq"] = JoinQueue(cfg.db_path, _get_client, _notify)
    app.bot_data["bc"] = BroadcastWorker(cfg.db_path, _get_client, _notify)
    app.bot_data["superbc"] = SuperBroadcastWorker(cfg.db_path, sm, _notify)
    watcher = LinkWatcher(_get_client, _notify, cfg.db_path)
    watcher._enabled = database.kv_get(cfg.db_path, "watch_enabled", "0") == "1"
    app.bot_data["watcher"] = watcher

    join_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(join_start, pattern=r"^m:join$")],
        states={ASK_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_links),
                              CallbackQueryHandler(join_done, pattern=r"^j:done$"),
                              CallbackQueryHandler(join_lim, pattern=r"^j:lim(:\d+|keep)$")],
                ASK_JOIN_MODE: [CallbackQueryHandler(join_mode, pattern=r"^j:(auto|manual)$")],
                ASK_JOIN_IV: [CallbackQueryHandler(join_iv_btn, pattern=r"^j:ivs?:\d+$"),
                              MessageHandler(filters.TEXT & ~filters.COMMAND, join_iv_msg)]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern=r"^x:cancel$"),
                   CommandHandler("cancel", cancel_conv)],
        per_message=False,
        allow_reentry=True,
    )
    collect_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(collect_start, pattern=r"^m:collect$")],
        states={ASK_COLLECT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                                   collect_phone)]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern=r"^x:cancel$"),
                   CommandHandler("cancel", cancel_conv)],
        per_message=False,
        allow_reentry=True,
    )
    post_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(post_start, pattern=r"^m:post$")],
        states={ASK_AD: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_ad)],
                ASK_POST_IV: [CallbackQueryHandler(post_iv_btn, pattern=r"^p:iv:\d+$"),
                              MessageHandler(filters.TEXT & ~filters.COMMAND, post_iv_msg)],
                ASK_POST_GROUPS: [CallbackQueryHandler(post_toggle,
                                                       pattern=r"^p:(t:\d+|all|none|page:\d+|noop)$"),
                                  CallbackQueryHandler(post_begin,
                                                       pattern=r"^p:start$")]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern=r"^x:cancel$"),
                   CommandHandler("cancel", cancel_conv)],
        per_message=False,
        allow_reentry=True,
    )
    super_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(super_start, pattern=r"^m:super$")],
        states={
            ASK_SUPER_ACCS: [
                CallbackQueryHandler(super_toggle,
                                     pattern=r"^s:(acc:.+|all|none)$"),
                CallbackQueryHandler(super_next, pattern=r"^s:next$")],
            ASK_SUPER_AD: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                          super_ad)],
            ASK_SUPER_IV: [
                CallbackQueryHandler(super_iv_btn, pattern=r"^s:ivs?:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, super_iv_msg)],
            ASK_SUPER_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                             super_limit_msg)],
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern=r"^x:cancel$"),
                   CommandHandler("cancel", cancel_conv)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_conv))
    app.add_error_handler(on_error)

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^n:add$")],
        states={ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone)],
                ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_code),
                           CallbackQueryHandler(add_resend, pattern=r"^n:resend$")],
                ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_password)]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern=r"^x:cancel$"),
                   CommandHandler("cancel", cancel_conv)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(add_conv)
    app.add_handler(join_conv)
    app.add_handler(collect_conv)
    app.add_handler(post_conv)
    app.add_handler(super_conv)
    app.bot_data["convs"] = [add_conv, join_conv, collect_conv, post_conv,
                             super_conv]
    app.add_handler(CallbackQueryHandler(
        reset_flow_state,
        pattern=r"^(m:(menu|status|stop|watch|nums|post|join|collect|super)|n:(add|use|del)|s:stop)",
        group=-1))
    app.add_handler(CommandHandler("start", reset_flow_state), group=-1)
    app.add_handler(CommandHandler("cancel", reset_flow_state), group=-1)
    app.add_handler(CallbackQueryHandler(nums_menu, pattern=r"^m:nums$"))
    app.add_handler(CallbackQueryHandler(num_use, pattern=r"^n:use:"))
    app.add_handler(CallbackQueryHandler(num_del, pattern=r"^n:del:"))
    app.add_handler(CallbackQueryHandler(join_one, pattern=r"^j:one:\d+$"))
    app.add_handler(CallbackQueryHandler(join_noop, pattern=r"^j:noop$"))
    app.add_handler(CallbackQueryHandler(join_all, pattern=r"^j:all$"))
    app.add_handler(CallbackQueryHandler(post_stop, pattern=r"^p:stop$"))
    app.add_handler(CallbackQueryHandler(super_stop, pattern=r"^s:stop$"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:(menu|status|stop|watch)$"))
    app.add_handler(CallbackQueryHandler(stale_button))
    return app
