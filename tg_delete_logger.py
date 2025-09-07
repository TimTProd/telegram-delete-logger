#!/usr/bin/env python3

import logging
import pickle
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Union
import os
import re
from asyncio.locks import Event
from contextlib import contextmanager

from telethon import events

from telethon.events import NewMessage, MessageDeleted, MessageEdited
from telethon import TelegramClient
from telethon.hints import Entity
from telethon.tl.functions.messages import SaveGifRequest, SaveRecentStickerRequest
from telethon.tl.types import (
    DocumentAttributeFilename,
    Message,
    PeerUser,
    PeerChat,
    PeerChannel,
    Channel,
    Chat,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDice,
    MessageMediaWebPage,
    MessageMediaGame,
    Document,
    InputDocument,
    DocumentAttributeAnimated,
    MessageMediaPhoto,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaPoll,
    Photo,
    Contact,
    UpdateReadMessagesContents,
)
import config
import file_encrypt

# --- paths & sqlite datetime adapters ---
try:
    from pathlib import Path
    from datetime import datetime, date
    import sqlite3 as _sqlite3  # alias to avoid shadowing if 'sqlite3' imported above

    _BASE_DIR = Path(__file__).resolve().parent
    _DB_DIR = _BASE_DIR / "db"
    _MEDIA_DIR = _BASE_DIR / "media"
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    _SESSION_PATH = str(_DB_DIR / "user")

    # Python 3.12+ removes default datetime adapter; register explicit adapters.
    _sqlite3.register_adapter(datetime, lambda d: d.strftime('%Y-%m-%d %H:%M:%S'))
    _sqlite3.register_adapter(date, lambda d: d.strftime('%Y-%m-%d'))
except Exception as _e:
    # Fallbacks in case of very early import-time failures
    _SESSION_PATH = "db/user"
# ----------------------------------------


TYPE_USER = 1
TYPE_CHANNEL = 2
TYPE_GROUP = 3
TYPE_BOT = 4
TYPE_UNKNOWN = 0

client = TelegramClient(_SESSION_PATH, config.API_ID, config.API_HASH)
my_id = -1
sqlite_cursor: sqlite3.Cursor = None
sqlite_connection: sqlite3.Connection = None

# Resolve the target chat for logging robustly (supports LOG_CHAT, LOG_CHAT_ID, or defaults to 'me')
LOG_ENTITY = None
async def _resolve_log_entity():
    global LOG_ENTITY
    candidates = []
    if hasattr(config, "LOG_CHAT") and getattr(config, "LOG_CHAT"):
        candidates.append(getattr(config, "LOG_CHAT"))
    if hasattr(config, "LOG_CHAT_ID") and getattr(config, "LOG_CHAT_ID"):
        candidates.append(getattr(config, "LOG_CHAT_ID"))
    candidates.append("me")
    last_exc = None
    for cand in candidates:
        try:
            ent = await client.get_entity(cand)
            return ent
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Could not resolve a log chat/entity")

async def send_log(text=None, **kwargs):
    """Send a log message; auto-resolve entity and retry on cache misses."""
    global LOG_ENTITY
    if LOG_ENTITY is None:
        LOG_ENTITY = await _resolve_log_entity()
    try:
        return await client.send_message(LOG_ENTITY, text, **kwargs)
    except ValueError:
        # Cache miss / hash mismatch: resolve again and retry
        LOG_ENTITY = await _resolve_log_entity()
        return await client.send_message(LOG_ENTITY, text, **kwargs)



# --- timezone helpers ---
def _get_local_zoneinfo():
    tz_name = getattr(config, "TIMEZONE", "UTC")
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _to_local_timestr(value: "datetime|str") -> str:
    """Convert a UTC datetime (or 'YYYY-MM-DD HH:MM:SS' string assumed UTC)
    to configured timezone string 'YYYY-MM-DD HH:MM:SS'."""
    target_tz = _get_local_zoneinfo()
    dt: datetime
    if isinstance(value, str):
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except Exception:
            # best-effort parse
            try:
                dt = datetime.fromisoformat(value)
            except Exception:
                return value
        dt = dt.replace(tzinfo=timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        return str(value)

    local_dt = dt.astimezone(target_tz)
    return local_dt.strftime('%Y-%m-%d %H:%M:%S')


def init_db():
    if not _DB_DIR.exists():
        _DB_DIR.mkdir(parents=True, exist_ok=True)
    if not _MEDIA_DIR.exists():
        _MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(_DB_DIR / "messages.db"), detect_types=0, check_same_thread=False)
    cursor = connection.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER, from_id INTEGER, chat_id INTEGER,
                  type INTEGER, msg_text TEXT, media BLOB, noforwards INTEGER DEFAULT 0, self_destructing INTEGER DEFAULT 0, created_time TIMESTAMP, edited_time TIMESTAMP,
                  PRIMARY KEY (chat_id, id, edited_time))""")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS messages_created_index ON messages (created_time DESC)")

    connection.commit()

    return cursor, connection


async def get_chat_type(event: Event) -> int:
    chat_type = TYPE_UNKNOWN
    if event.is_group:  # chats and megagroups
        chat_type = TYPE_GROUP
    elif event.is_channel:  # megagroups and channels
        chat_type = TYPE_CHANNEL
    elif event.is_private:
        if (await event.get_sender()).bot:
            chat_type = TYPE_BOT
        else:
            chat_type = TYPE_USER
    return chat_type


async def new_message_handler(event: Union[NewMessage.Event, MessageEdited.Event]):
    chat_id = event.chat_id
    from_id = get_sender_id(event.message)
    msg_id = event.message.id

    if chat_id == config.LOG_CHAT_ID and from_id == my_id and \
            event.message.text and \
            (re.match(r"^(https:\/\/)?t\.me\/(?:c\/)?[\d\w]+\/[\d]+", event.message.text) or
             re.match(
                 r"^tg:\/\/openmessage\?user_id=\d+&message_id=\d+", event.message.text)
             ):
        msg_links = re.findall(
            r"(?:https:\/\/)?t\.me\/(?:c\/)?[\d\w]+\/[\d]+", event.message.text)
        if not msg_links:
            msg_links = re.findall(
                r"tg:\/\/openmessage\?user_id=\d+&message_id=\d+", event.message.text)
        if msg_links:
            for msg_link in msg_links:
                await save_restricted_msg(msg_link)
            return

    if from_id in config.IGNORED_IDS or chat_id in config.IGNORED_IDS:
        return

    edited_time = 0
    noforwards = False
    self_destructing = False

    try:
        noforwards = event.chat.noforwards is True
    except AttributeError:  # AttributeError: 'User' object has no attribute 'noforwards'
        noforwards = event.message.noforwards is True

    # noforwards = False  # wtf why does it work now?

    try:
        if event.message.media.ttl_seconds:
            self_destructing = True
    except AttributeError:
        pass

    if event.message.media and (noforwards or self_destructing):
        await save_media_as_file(event.message)

    async with asyncio.Lock():
        if isinstance(event, MessageEdited.Event):
            edited_time = datetime.now()  # event.message.edit_date
            await edited_deleted_handler(event)

        sqlite_cursor.execute(
            "INSERT INTO messages (id, from_id, chat_id, edited_time, type, msg_text, media, noforwards, self_destructing, created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                from_id,
                chat_id,
                edited_time,
                await get_chat_type(event),
                event.message.message,
                sqlite3.Binary(pickle.dumps(event.message.media)),
                int(noforwards),
                int(self_destructing),
                datetime.now(timezone.utc))
        )
        sqlite_connection.commit()


def get_sender_id(message) -> int:
    from_id = 0
    if isinstance(message.peer_id, PeerUser):
        from_id = my_id if message.out else message.peer_id.user_id
    elif isinstance(message.peer_id, PeerChannel):
        if isinstance(message.from_id, PeerUser):
            from_id = message.from_id.user_id
        if isinstance(message.from_id, PeerChannel):
            from_id = message.from_id.channel_id
    elif isinstance(message.peer_id, PeerChat):
        if isinstance(message.from_id, PeerUser):
            from_id = message.from_id.user_id
        if isinstance(message.from_id, PeerChannel):
            from_id = message.from_id.channel_id

    return from_id


def load_messages_from_event(event: Union[MessageDeleted.Event, MessageEdited.Event, UpdateReadMessagesContents]) -> List[Message]:
    if isinstance(event, MessageDeleted.Event):
        ids = event.deleted_ids[:config.RATE_LIMIT_NUM_MESSAGES]
    if isinstance(event, UpdateReadMessagesContents):
        ids = event.messages[:config.RATE_LIMIT_NUM_MESSAGES]
    elif isinstance(event, MessageEdited.Event):
        ids = [event.message.id]

    sql_message_ids = ",".join(str(deleted_id) for deleted_id in ids)
    if hasattr(event, "chat_id") and event.chat_id:
        where_clause = f"WHERE chat_id = {event.chat_id} and id IN ({sql_message_ids})"
    else:
        where_clause = f"WHERE chat_id not like \"-100%\" and id IN ({sql_message_ids})"
    query = f"""SELECT * FROM (SELECT id, from_id, chat_id, msg_text, media, noforwards, self_destructing,
            created_time FROM messages {where_clause} ORDER BY edited_time DESC)
            GROUP BY chat_id, id ORDER BY created_time ASC"""

    db_results = sqlite_cursor.execute(query).fetchall()

    messages = []
    for db_result in db_results:
        # skip read messages which are not self-destructing
        if isinstance(event, UpdateReadMessagesContents) and not db_result[6]:
            continue

        messages.append({
            "id": db_result[0],
            "from_id": db_result[1],
            "chat_id": db_result[2],
            "msg_text": db_result[3],
            "media": pickle.loads(db_result[4]),
            "noforwards": db_result[5],
            "self_destructing": db_result[6],
            "created_time": db_result[7],
        })

    return messages


async def create_mention(entity_id, chat_msg_id: int = None) -> str:
    if chat_msg_id is None:
        msg_id = 1
    else:
        msg_id = chat_msg_id

    if entity_id == 0:
        return "Unknown"

    try:
        entity: Entity = await client.get_entity(entity_id)

        if isinstance(entity, (Channel, Chat)):
            name = entity.title
            chat_id = str(entity_id).replace("-100", "")
            mention = f"[{name}](t.me/c/{chat_id}/{msg_id})"
        else:
            if entity.first_name:
                is_pm = chat_msg_id is not None
                name = \
                    (entity.first_name + " " if entity.first_name else "") + \
                    (entity.last_name if entity.last_name else "")

                mention = f"[{name}](tg://user?id={entity.id})" + \
                    (" #pm" if is_pm else "")
            elif entity.username:
                mention = f"[@{entity.username}](t.me/{entity.username})"
            elif entity.phone:
                mention = entity.phone
            else:
                mention = entity.id
    except Exception as e:
        logging.warning(e)
        mention = str(entity_id)

    return mention


async def edited_deleted_handler(event: Union[MessageDeleted.Event, MessageEdited.Event, UpdateReadMessagesContents]):
    if not isinstance(event, MessageDeleted.Event) and not isinstance(event, MessageEdited.Event) and not isinstance(event, UpdateReadMessagesContents):
        return

    if isinstance(event, MessageEdited.Event) and not config.SAVE_EDITED_MESSAGES:
        return

    messages = load_messages_from_event(event)

    log_deleted_sender_ids = []

    for message in messages:
        if message['from_id'] in config.IGNORED_IDS or message['chat_id'] in config.IGNORED_IDS:
            return

        mention_sender = await create_mention(message['from_id'])
        mention_chat = await create_mention(message['chat_id'], message['id'])

        log_deleted_sender_ids.append(message['from_id'])

        if isinstance(event, MessageDeleted.Event) or isinstance(event, UpdateReadMessagesContents):
            if isinstance(event, MessageDeleted.Event):
                text = f"**Deleted message from: **{mention_sender}\n"
            if isinstance(event, UpdateReadMessagesContents):
                text = f"**Deleted #selfdestructing message from: **{mention_sender}\n"

            text += f"in {mention_chat}\n"

            if 'created_time' in message and message['created_time']:
                created_time_value = message['created_time']
                created_time_str = _to_local_timestr(created_time_value)
                text += f"**send time:** {created_time_str}\n"

            if message['msg_text']:
                text += "**Message:** \n" + message['msg_text']
        elif isinstance(event, MessageEdited.Event):
            text = f"**✏Edited message from: **{mention_sender}\n"

            text += f"in {mention_chat}\n"

            if 'created_time' in message and message['created_time']:
                created_time_value = message['created_time']
                created_time_str = _to_local_timestr(created_time_value)
                text += f"**send time:** {created_time_str}\n"

            if getattr(event.message, 'edit_date', None):
                edit_time_str = _to_local_timestr(event.message.edit_date)
                text += f"**edit time:** {edit_time_str}\n"

            if message['msg_text']:
                text += f"**Original message:**\n{message['msg_text']}\n\n"
            if event.message.text:
                text += f"**Edited message:**\n{event.message.text}"

        is_sticker = hasattr(message['media'], "document") and \
            message['media'].document.attributes and \
            any(isinstance(attr, DocumentAttributeSticker)
                for attr in message['media'].document.attributes)
        is_gif = hasattr(message['media'], "document") and \
            message['media'].document.attributes and \
            any(isinstance(attr, DocumentAttributeAnimated)
                for attr in message['media'].document.attributes)
        is_round_video = hasattr(message['media'], "document") and \
            message['media'].document.attributes and \
            any((isinstance(attr, DocumentAttributeVideo) and attr.round_message is True)
                for attr in message['media'].document.attributes)
        is_dice = isinstance(message['media'], MessageMediaDice)
        is_instant_view = isinstance(message['media'], MessageMediaWebPage)
        is_game = isinstance(message['media'], MessageMediaGame)
        is_geo = isinstance(message['media'], MessageMediaGeo)
        is_poll = isinstance(message['media'], MessageMediaPoll)
        is_contact = isinstance(
            message['media'], (MessageMediaContact, Contact))

        with retrieve_media_as_file(message['id'], message['chat_id'], message['media'], message['noforwards'] or message['self_destructing']) as media_file:

            if is_sticker or is_round_video or is_dice or is_game or is_contact or is_geo or is_poll:
                sent_msg = await send_log( file=media_file)
                await sent_msg.reply(text)
            elif is_instant_view:
                await send_log( text)
            else:
                await send_log( text, file=media_file)

        if is_gif and config.DELETE_SENT_GIFS_FROM_SAVED:
            await delete_from_saved_gifs(message['media'].document)

        if is_sticker and config.DELETE_SENT_STICKERS_FROM_SAVED:
            await delete_from_saved_stickers(message['media'].document)

    if isinstance(event, MessageDeleted.Event):
        ids = event.deleted_ids
        event_verb = "deleted"
    elif isinstance(event, UpdateReadMessagesContents):
        ids = event.messages
        event_verb = "self destructed"
    elif isinstance(event, MessageEdited.Event):
        ids = [event.message.id]
        event_verb = "edited"

    if len(ids) > config.RATE_LIMIT_NUM_MESSAGES and log_deleted_sender_ids:
        await send_log( f"{len(ids)} messages {event_verb}. Logged {config.RATE_LIMIT_NUM_MESSAGES}.")

    logging.info(
        f"Got 1 {event_verb} message. DB has {len(messages)}. Users: {', '.join(str(_id) for _id in log_deleted_sender_ids)}"
    )


def get_file_name(media) -> str:
    if media:
        try:
            file_name = [attr for attr in media.document.attributes if isinstance(
                attr, DocumentAttributeFilename)][0].file_name
        except Exception as e:
            try:
                mime_type = media.document.mime_type
            except (NameError, AttributeError) as e:
                mime_type = None

            if mime_type == "audio/ogg":
                file_name = "voicenote.ogg"
            elif mime_type == "video/mp4":
                file_name = "video.mp4"
            elif isinstance(media, (MessageMediaPhoto, Photo)):
                file_name = 'photo.jpg'
            elif isinstance(media, (MessageMediaContact, Contact)):
                file_name = 'contact.vcf'
            else:
                file_name = "file.unknown"

        return file_name
    else:
        return None


async def save_restricted_msg(link: str):
    if link.startswith("tg://"):
        parts = re.findall(r"\d+", link)
        if len(parts) == 2:
            chat_id = int(parts[0])
            msg_id = int(parts[1])
        else:
            await send_log( f"Could not parse link: {link}")
            return
    else:
        parts = link.split('/')
        msg_id = int(parts[-1])
        chat_id = int(parts[-2]) if parts[-2].isdigit() else parts[-2]

    msg = await client.get_messages(chat_id, ids=msg_id)
    chat_id = msg.chat_id
    from_id = get_sender_id(msg)

    mention_sender = await create_mention(from_id)
    mention_chat = await create_mention(chat_id, msg_id)

    text = f"**↗️Saved message from: **{mention_sender}\n"

    text += f"in {mention_chat}\n"

    # include original message date/time
    try:
        if getattr(msg, 'date', None):
            text += f"**send time:** {_to_local_timestr(msg.date)}\n"
    except Exception:
        pass

    if msg.text:
        text += "**Message:** \n" + msg.text

    try:
        if msg.media:
            await save_media_as_file(msg)
            with retrieve_media_as_file(msg_id, chat_id, msg.media, True) as f:
                await send_log( text, file=f)
        else:
            await send_log( text)
    except Exception as e:
        await send_log( str(e))


async def save_media_as_file(msg: Message):
    msg_id = msg.id
    chat_id = msg.chat_id

    if msg.media:
        if msg.file and msg.file.size > config.MAX_IN_MEMORY_FILE_SIZE:
            raise Exception(f"File too large to save ({msg.file.size} bytes)")
        file_path = str(_MEDIA_DIR / f"{msg_id}_{chat_id}")

        with file_encrypt.encrypted(file_path) as f:
            await client.download_media(msg.media, f)


@contextmanager
def retrieve_media_as_file(msg_id: int, chat_id: int, media, noforwards: bool):
    file_name = get_file_name(media)
    file_path = str(_MEDIA_DIR / f"{msg_id}_{chat_id}")

    if noforwards and\
            not isinstance(media, MessageMediaGeo) and\
            not isinstance(media, MessageMediaPoll):
        with file_encrypt.decrypted(file_path) as f:
            f.name = file_name
            yield f
    else:
        yield media


async def delete_from_saved_gifs(gif: Document):
    await client(SaveGifRequest(
        id=InputDocument(
            id=gif.id,
            access_hash=gif.access_hash,
            file_reference=gif.file_reference
        ),
        unsave=True
    ))


async def delete_from_saved_stickers(sticker: Document):
    await client(SaveRecentStickerRequest(
        id=InputDocument(
            id=sticker.id,
            access_hash=sticker.access_hash,
            file_reference=sticker.file_reference
        ),
        unsave=True
    ))


async def delete_expired_messages():
    while True:
        now = datetime.now()
        time_user = now - timedelta(days=config.PERSIST_TIME_IN_DAYS_USER)
        time_channel = now - \
            timedelta(days=config.PERSIST_TIME_IN_DAYS_CHANNEL)
        time_group = now - timedelta(days=config.PERSIST_TIME_IN_DAYS_GROUP)
        time_bot = now - timedelta(days=config.PERSIST_TIME_IN_DAYS_BOT)
        time_unknown = now - timedelta(days=config.PERSIST_TIME_IN_DAYS_GROUP)

        sqlite_cursor.execute(
            """DELETE FROM messages WHERE (type = ? and created_time < ?) OR
            (type = ? and created_time < ?) OR
            (type = ? and created_time < ?) OR
            (type = ? and created_time < ?) OR
            (type = ? and created_time < ?)""",
            (TYPE_USER, time_user,
             TYPE_CHANNEL, time_channel,
             TYPE_GROUP, time_group,
             TYPE_BOT, time_bot,
             TYPE_UNKNOWN, time_unknown,
             ))

        if sqlite_cursor.rowcount > 0:
            logging.info(
                f"Deleted {sqlite_cursor.rowcount} expired messages from DB"
            )

        # todo: save group/channel label in file name

        num_files_deleted = 0
        file_persist_days = max(
            config.PERSIST_TIME_IN_DAYS_GROUP, config.PERSIST_TIME_IN_DAYS_CHANNEL)
        for (dirpath, dirnames, filenames) in os.walk("media"):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                modified_time = datetime.fromtimestamp(
                    os.path.getmtime(file_path))
                expiry_time = now - timedelta(days=file_persist_days)
                if modified_time < expiry_time:
                    os.unlink(file_path)
                    num_files_deleted += 1

        if num_files_deleted > 0:
            logging.info(f"Deleted {num_files_deleted} expired files")

        await asyncio.sleep(300)


async def init():
    global my_id

    if config.DEBUG_MODE:
        logging.basicConfig(level="INFO")
    else:
        logging.basicConfig(level="WARNING")

    config.IGNORED_IDS.add(config.LOG_CHAT_ID)

    my_id = (await client.get_me()).id

    client.add_event_handler(new_message_handler, events.NewMessage(
        incoming=True, outgoing=config.LISTEN_OUTGOING_MESSAGES))
    client.add_event_handler(new_message_handler, events.MessageEdited())
    client.add_event_handler(edited_deleted_handler, events.MessageEdited())
    client.add_event_handler(edited_deleted_handler, events.MessageDeleted())
    client.add_event_handler(edited_deleted_handler)
    # client.add_event_handler(edited_deleted_handler,
    #                          events.MessageRead(True))
    # doesnt work for self destructs

    await delete_expired_messages()

if __name__ == "__main__":
    sqlite_cursor, sqlite_connection = init_db()

    with client:
        client.loop.run_until_complete(init())
