import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Get token from environment for safety
TOKEN = os.environ.get("TG_BOT_TOKEN")  # set this before running
if not TOKEN:
    raise SystemExit("Set TG_BOT_TOKEN environment variable")

logging.basicConfig(level=logging.INFO)

# In-memory storage
tracked = {
    "chat_id": None,
    "message_id": None,
    "next_index": 1,
    "entries": [],  # list of (idx, user_id, display_name)
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "Reply to this message to add yourself to the list."
    )
    # try to pin (may require bot admin rights)
    try:
        await msg.pin()
    except Exception:
        pass
    tracked["chat_id"] = msg.chat.id
    tracked["message_id"] = msg.message_id
    tracked["next_index"] = 1
    tracked["entries"].clear()
    await update.message.reply_text("Tracked list message set (in-memory).")


async def reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    if tracked["chat_id"] is None:
        return
    # only respond to replies to the tracked message
    if (
        msg.chat.id != tracked["chat_id"]
        or msg.reply_to_message.message_id != tracked["message_id"]
    ):
        return
    user = msg.from_user
    display = (
        user.username
        or f"{(user.first_name or '')} {(user.last_name or '')}".strip()
        or f"user{user.id}"
    )
    # append new entry (allow duplicates)
    idx = tracked["next_index"]
    tracked["entries"].append((idx, user.id, display))
    tracked["next_index"] += 1
    # render and edit the tracked message
    lines = [f"{e[0]}. {e[2]}" for e in tracked["entries"]]
    body = "Current list:\n" + "\n".join(lines) if lines else "Current list: (empty)"
    try:
        await context.bot.edit_message_text(
            body, chat_id=tracked["chat_id"], message_id=tracked["message_id"]
        )
    except Exception:
        logging.exception("Failed to edit tracked message")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
