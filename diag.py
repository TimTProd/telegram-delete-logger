#!/usr/bin/env python3
import asyncio
import logging
from telethon import TelegramClient, events

import config  # expects: API_ID, API_HASH, BOT_TOKEN, and optionally LOG_CHAT or LOG_CHAT_ID

async def main():
    logging.basicConfig(level=logging.INFO)
    api_id = config.API_ID
    api_hash = config.API_HASH
    bot = TelegramClient('db/bot_diag', api_id, api_hash)

    # Start bot session
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    print(f"Bot online as @{getattr(me, 'username', None)} (id={me.id})")

    # Try to resolve the LOG target and send a test message
    target = None
    candidate = getattr(config, 'LOG_CHAT', None)
    if candidate is None and hasattr(config, 'LOG_CHAT_ID'):
        candidate = getattr(config, 'LOG_CHAT_ID')

    if candidate is not None:
        try:
            target = await bot.get_entity(candidate)
            title = getattr(target, 'title', getattr(target, 'username', str(getattr(target, 'id', 'unknown'))))
            print(f"Resolved LOG target: {title} (id={getattr(target, 'id', None)})")
            await bot.send_message(target, "✅ Bot connectivity test: I can post here.")
            print("Sent a test message to the LOG target.")
        except Exception as e:
            print(f"Failed to resolve/send to LOG target {candidate!r}: {e}")

    # Listen to what the bot actually receives
    @bot.on(events.NewMessage())
    async def on_new(event):
        chat = await event.get_chat()
        title = getattr(chat, 'title', getattr(chat, 'username', 'DM'))
        preview = (event.raw_text or "")[:120].replace("\n", " ")
        print(f"[NewMessage] chat_id={event.chat_id} title={title!r} text={preview!r}")

    @bot.on(events.MessageDeleted())
    async def on_deleted(event):
        print(f"[MessageDeleted] chat_id={event.chat_id} deleted_ids={event.deleted_ids}")

    print("Listening for updates the bot is allowed to see… Press Ctrl+C to stop.")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

