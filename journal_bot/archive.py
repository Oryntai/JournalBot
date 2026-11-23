import re
from datetime import datetime

from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

MEDIA_LABELS = {
    "animation": "анимация", "photo": "фото", "video": "видео", "voice": "голосовое",
    "video_note": "видеосообщение", "audio": "аудио", "document": "файл", "sticker": "стикер",
}
FILE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def one_line(text, limit):
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def unix_time(value):
    return int(value.timestamp()) if isinstance(value, datetime) else int(value)


def restricted(item):
    return any(getattr(item, field, None) for field in (
        "has_protected_content", "ephemeral_message_id", "is_paid_post", "paid_media",
        "ttl_seconds", "self_destruct_type", "is_view_once",
    ))


def snapshot(message: Message, owner_id):
    text = message.text or message.caption or ""
    sender = message.from_user
    peer = " ".join(filter(None, (message.chat.first_name, message.chat.last_name)))
    media = None
    for kind in MEDIA_LABELS:
        if kind == "photo":
            item = message.photo[-1] if message.photo else None
        else:
            item = getattr(message, kind, None)
        if item is None or not FILE_ID.fullmatch(item.file_id):
            continue
        if restricted(item):
            return None
        media = {"type": kind, "id": item.file_id, "uniqueId": item.file_unique_id,
                 "spoiler": bool(message.has_media_spoiler)}
        break
    if not text and not media:
        return None
    return {"text": text, "peer": one_line(peer or message.chat.username or "Личный чат", 60),
            "media": media, "outgoing": bool(sender and sender.id == owner_id)}


def same_content(first, second):
    if first is None or first["text"] != second["text"]:
        return False
    fields = ("type", "uniqueId", "spoiler")
    return all((first["media"] or {}).get(field) == (second["media"] or {}).get(field) for field in fields)


class Archive:
    def __init__(self, store, owner_id, retention):
        self.store = store
        self.owner_id = owner_id
        self.retention = retention

    def connection(self, connection: BusinessConnection):
        if connection.user.id != self.owner_id:
            return
        current = self.store.setting("connection")
        if connection.is_enabled and connection.id != current:
            self.disconnect()
            self.store.set_setting("connection", connection.id)
        elif not connection.is_enabled and connection.id == current:
            self.disconnect()

    def disconnect(self):
        self.purge()
        self.store.set_setting("connection", None)

    def purge(self):
        self.store.clear()
        self.pause()

    def pause(self):
        self.store.set_setting("enabledAt", None)

    def enable(self, now):
        if not self.store.setting("connection"):
            return False
        if self.store.setting("enabledAt") is None:
            self.store.set_setting("enabledAt", now)
        return True

    def captures(self, connection_id, chat):
        return (bool(connection_id) and connection_id == self.store.setting("connection")
                and self.store.setting("enabledAt") is not None and chat.type == "private" and chat.id > 0)

    def record(self, message: Message, kind, now):
        connection_id = message.business_connection_id
        if not self.captures(connection_id, message.chat) or message.message_id <= 0:
            return
        key = self.store.message_key(connection_id, message.chat.id, message.message_id)
        if restricted(message) or restricted(message.chat):
            self.store.forget(key)
            return
        sent = unix_time(message.date)
        if sent < self.store.setting("enabledAt"):
            return
        current = snapshot(message, self.owner_id)
        if current is None:
            return
        previous = self.store.latest(key)
        expires = previous.expires if previous else sent + self.retention
        at = unix_time(message.edit_date or message.date)
        if expires <= now or (previous and (kind == "new" or previous.body["kind"] == "delete" or at < previous.body["at"])):
            return
        before = previous.body["after"] if previous else None
        if same_content(before, current):
            return
        self.store.append(self.store.chat_key(connection_id, message.chat.id), key, expires,
                          {"kind": kind, "at": at, "messageDate": sent, "before": before, "after": current})

    def remove(self, update: BusinessMessagesDeleted, now):
        connection_id = update.business_connection_id
        if not self.captures(connection_id, update.chat):
            return
        chat = self.store.chat_key(connection_id, update.chat.id)
        for message_id in update.message_ids:
            key = self.store.message_key(connection_id, update.chat.id, message_id)
            previous = self.store.latest(key)
            if previous and previous.expires > now and previous.body["after"]:
                self.store.append(chat, key, previous.expires, {"kind": "delete", "at": now,
                                  "messageDate": previous.body["messageDate"], "before": previous.body["after"],
                                  "after": None})
