import asyncio
import html
import os
import sys
import time

import aiosqlite
from telegram import ChatMemberUpdated, Update
from telegram.ext import (
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# from datetime import datetime, timezone


TOKEN = os.getenv("TG_TOKEN")
if not TOKEN:
    print("Error: TG_TOKEN environment variable not set", file=sys.stderr)
    sys.exit(1)
DB = "members_normalized.db"
BATCH_SIZE = 25
DELAY_SEC = 1.2
STALE_DAYS = 90  # optional prune threshold


# --- DB init / migrations ---
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                updated_at INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS membership (
                chat_id INTEGER,
                user_id INTEGER,
                first_seen INTEGER,
                last_seen INTEGER,
                PRIMARY KEY(chat_id, user_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_membership_chat ON membership(chat_id)"
        )
        await db.commit()


# --- DB helpers ---
def now_ts() -> int:
    return int(time.time())


async def upsert_user(user):
    if user is None:
        return
    name = user.full_name or user.first_name or (user.username or f"user{user.id}")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO users(user_id,name,username,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (user.id, name, user.username or "", now_ts()),
        )
        await db.commit()


async def upsert_membership(chat_id: int, user):
    if user is None:
        return
    ts = now_ts()
    await upsert_user(user)
    async with aiosqlite.connect(DB) as db:
        # Insert membership or update last_seen
        await db.execute(
            """
            INSERT INTO membership(chat_id,user_id,first_seen,last_seen)
            VALUES(?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                last_seen=excluded.last_seen
            """,
            (chat_id, user.id, ts, ts),
        )
        await db.commit()


async def remove_membership(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "DELETE FROM membership WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        await db.commit()


async def get_members_by_chat(chat_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """
            SELECT m.user_id, u.name, u.username, m.first_seen, m.last_seen
            FROM membership m
            JOIN users u ON u.user_id = m.user_id
            WHERE m.chat_id=?
            ORDER BY m.last_seen DESC
            """,
            (chat_id,),
        )
        rows = await cur.fetchall()
        return rows


async def prune_stale(threshold_days: int = STALE_DAYS):
    cutoff = now_ts() - threshold_days * 86400
    async with aiosqlite.connect(DB) as db:
        # remove membership rows last_seen < cutoff
        await db.execute("DELETE FROM membership WHERE last_seen < ?", (cutoff,))
        # optional: remove users not referenced in membership
        await db.execute(
            "DELETE FROM users WHERE user_id NOT IN (SELECT DISTINCT user_id FROM membership)"
        )
        await db.commit()


# --- Bot handlers ---
async def message_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_user:
        await upsert_membership(update.effective_chat.id, update.effective_user)


async def chat_member_update_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    # handle both ChatMemberUpdated fields safely
    cm = None
    if hasattr(update, "chat_member") and update.chat_member is not None:
        cm = update.chat_member
    elif hasattr(update, "my_chat_member") and update.my_chat_member is not None:
        cm = update.my_chat_member
    else:
        return  # nothing to do

    chat = update.effective_chat
    if not chat or cm.new_chat_member is None:
        return

    user = cm.new_chat_member.user
    status = cm.new_chat_member.status

    if status in ("member", "creator", "administrator"):
        await upsert_membership(chat.id, user)
    elif status in ("left", "kicked"):
        await remove_membership(chat.id, user.id)


async def everyone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    # require admin
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("Only group admins can use /everyone.")
        return

    # ensure admins included
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for adm in admins:
            await upsert_membership(chat.id, adm.user)
    except Exception:
        pass

    rows = await get_members_by_chat(chat.id)
    if not rows:
        await update.message.reply_text(
            "No known members. The bot hasn't seen anyone yet."
        )
        return

    mentions = []
    for user_id, name, username, first_seen, last_seen in rows:
        display = html.escape(name or (username or f"user{user_id}"))
        mentions.append(f'<a href="tg://user?id={user_id}">{display}</a>')

    chunks = [mentions[i : i + BATCH_SIZE] for i in range(0, len(mentions), BATCH_SIZE)]
    for chunk in chunks:
        text = " ".join(chunk)
        await update.message.reply_html(text, disable_web_page_preview=True)
        await asyncio.sleep(DELAY_SEC)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I will record users who send messages or trigger membership changes. Admins can use /everyone."
    )


async def prune_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin-only prune command
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("Only group admins can run /prune.")
        return

    await prune_stale()
    await update.message.reply_text(f"Pruned entries older than {STALE_DAYS} days.")


# --- Entry point ---
if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("everyone", everyone))
    app.add_handler(CommandHandler("prune", prune_cmd))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_collector))
    app.add_handler(
        ChatMemberHandler(
            chat_member_update_handler,
            chat_member_types=ChatMemberHandler.ANY_CHAT_MEMBER,
        )
    )

    app.run_polling()
