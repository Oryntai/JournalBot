import asyncio
import logging
import time
from html import escape

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message

from .archive import Archive, MEDIA_LABELS
from .screens import (ARCHIVE_BUTTON, CHATS_BUTTON, SETTINGS_BUTTON, Page, Screen, Screens,
                      cursor_value, id_value)
from .store import MAX_ID

LOG = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class JournalBot:
    def __init__(self, bot, store, owner_id, retention, timezone, username):
        self.bot, self.store, self.owner_id = bot, store, owner_id
        self.username = username.casefold()
        self.archive = Archive(store, owner_id, retention)
        self.screens = Screens(store, retention, timezone)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)
        self.router.business_connection.register(self.business_connection)
        self.router.business_message.register(self.business_message)
        self.router.edited_business_message.register(self.edited_business_message)
        self.router.deleted_business_messages.register(self.deleted_business_messages)

    def owner_chat(self, message):
        return (isinstance(message, Message) and message.chat.type == "private"
                and message.chat.id == self.owner_id and not message.business_connection_id)

    async def send(self, screen, silent=False):
        return await self.bot.send_message(self.owner_id, screen.text, parse_mode="HTML", protect_content=True,
                                           disable_notification=silent, link_preview_options=NO_PREVIEW,
                                           reply_markup=screen.keyboard)

    async def edit(self, message, screen):
        try:
            await self.bot.edit_message_text(text=screen.text, chat_id=self.owner_id, message_id=message.message_id,
                                             parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                             reply_markup=screen.keyboard if isinstance(screen.keyboard, InlineKeyboardMarkup) else None)
        except TelegramBadRequest as error:
            reason = error.message.lower()
            if "message is not modified" in reason:
                return
            if "message can't be edited" not in reason and "message to edit not found" not in reason:
                raise
            await self.send(screen)

    async def message(self, message, event_update, now):
        if not self.owner_chat(message) or not message.from_user or message.from_user.id != self.owner_id:
            return
        with self.store.update(event_update.update_id, now) as fresh:
            if not fresh:
                return
            screen = self.command(message.text or "", now)
        if screen:
            await self.send(screen)

    def command(self, text, now):
        menu = {ARCHIVE_BUTTON: self.screens.archive, CHATS_BUTTON: self.screens.chats,
                SETTINGS_BUTTON: self.screens.settings}
        if text in menu:
            return menu[text]()
        tokens = text.split()
        if not tokens:
            return None
        head, *args = tokens
        command, _, target = head.partition("@")
        if target and target.casefold() != self.username:
            return None
        match command:
            case "/start" | "/help":
                return self.screens.start()
            case "/archive":
                try:
                    if len(args) > 2:
                        raise ValueError
                    page = Page.parse(args[0] if args else "all", "all", args[1] if len(args) == 2 else "0")
                except ValueError:
                    page = Page()
                return self.screens.archive(page)
            case "/chats":
                try:
                    return self.screens.chats(cursor_value(args[0]) if len(args) == 1 else MAX_ID)
                except ValueError:
                    return self.screens.chats()
            case "/show":
                try:
                    return self.screens.entry(id_value(args[0])) if len(args) == 1 else self.screens.archive()
                except ValueError:
                    return self.screens.archive()
            case "/settings" | "/status":
                return self.screens.settings()
            case "/privacy":
                return self.screens.privacy()
            case "/enable":
                return self.set_recording(True, now)
            case "/disable":
                return self.set_recording(False, now)
            case "/purge":
                return self.purge() if args == ["confirm"] else self.screens.confirm_clear()
            case _:
                return None

    def set_recording(self, enabled, now):
        if enabled:
            self.archive.enable(now)
        else:
            self.archive.pause()
        return self.screens.settings()

    def purge(self):
        self.archive.purge()
        return self.screens.settings("Архив очищен.")

    async def callback(self, callback, event_update, now):
        if callback.from_user.id != self.owner_id or not self.owner_chat(callback.message):
            return
        fields = (callback.data or "").split(":")
        if fields[0] != "j":
            return
        with self.store.update(event_update.update_id, now) as fresh:
            if not fresh:
                return
            try:
                response = self.callback_action(fields[1:], now)
            except ValueError:
                response = None
        try:
            await callback.answer("Кнопка устарела. Открой меню заново." if response is None else None)
        except TelegramBadRequest:
            # The click may have waited while the bot was offline.
            pass
        if isinstance(response, Screen):
            await self.edit(callback.message, response)
        elif response is not None:
            is_media, entry_id, side = response
            if is_media:
                await self.attachment(entry_id, side)
            else:
                await self.full_text(entry_id, side)

    def callback_action(self, fields, now):
        match fields:
            case ["a", chat, kind, before]:
                return self.screens.archive(Page.parse(chat, kind, before))
            case ["e", entry, chat, kind, before]:
                return self.screens.entry(id_value(entry), Page.parse(chat, kind, before))
            case ["c", before]:
                return self.screens.chats(cursor_value(before))
            case ["settings"]:
                return self.screens.settings()
            case ["privacy"]:
                return self.screens.privacy()
            case ["clear"]:
                return self.screens.confirm_clear()
            case ["purge"]:
                return self.purge()
            case ["enable"]:
                return self.set_recording(True, now)
            case ["pause"]:
                return self.set_recording(False, now)
            case ["m" | "t" as action, entry, "before" | "after" as side]:
                return action == "m", id_value(entry), side
            case _:
                return None

    def version(self, entry_id, side):
        entry = self.store.event(entry_id)
        return entry.body[side] if entry else None

    async def attachment(self, entry_id, side):
        version = self.version(entry_id, side)
        media = version["media"] if version else None
        if not media or media["type"] not in MEDIA_LABELS:
            await self.send(Screen("Вложение больше недоступно."))
            return
        kind = media["type"]
        parameters = {kind: media["id"]}
        if media["spoiler"] and kind in {"photo", "video", "animation"}:
            parameters["has_spoiler"] = True
        try:
            await getattr(self.bot, f"send_{kind}")(chat_id=self.owner_id, protect_content=True, **parameters)
        except TelegramBadRequest:
            await self.send(Screen("Telegram не смог повторно отправить это вложение."))

    async def full_text(self, entry_id, side):
        version = self.version(entry_id, side)
        text = version["text"] if version else ""
        if not text:
            await self.send(Screen("Текст больше недоступен."))
            return
        for offset in range(0, len(text), 1800):
            part = text[offset:offset + 1800]
            if part.strip():
                await self.send(Screen(escape(part)))

    async def business_connection(self, business_connection, event_update, now):
        with self.store.update(event_update.update_id, now) as fresh:
            if fresh:
                self.archive.connection(business_connection)

    async def business_message(self, business_message, event_update, now):
        with self.store.update(event_update.update_id, now) as fresh:
            if fresh:
                self.archive.record(business_message, "new", now)

    async def edited_business_message(self, edited_business_message, event_update, now):
        with self.store.update(event_update.update_id, now) as fresh:
            if fresh:
                self.archive.record(edited_business_message, "edit", now)

    async def deleted_business_messages(self, deleted_business_messages, event_update, now):
        with self.store.update(event_update.update_id, now) as fresh:
            if fresh:
                self.archive.remove(deleted_business_messages, now)

    async def restore(self):
        current = self.store.setting("connection")
        if not current:
            return
        try:
            connection = await self.bot.get_business_connection(business_connection_id=current, request_timeout=12)
        except TelegramBadRequest as error:
            reason = error.message.casefold()
            if not any(text in reason for text in ("business connection not found", "business connection is invalid",
                                                    "business connection invalid", "business_connection_invalid")):
                raise
            with self.store.transaction():
                self.archive.disconnect()
            return
        with self.store.transaction():
            self.archive.connection(connection)

    async def poll(self):
        dispatcher = Dispatcher(disable_fsm=True)
        dispatcher.include_router(self.router)
        allowed = dispatcher.resolve_used_update_types()
        offset, last_received, notify_after, retry_delay = None, time.time(), 0, 1
        while True:
            now = int(time.time())
            self.store.prune(now, self.archive.retention)
            if now - last_received >= 3600:
                offset = None
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=20, request_timeout=35, allowed_updates=allowed)
            except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as error:
                LOG.warning("Polling retry (%s)", type(error).__name__)
                await asyncio.sleep(max(retry_delay, getattr(error, "retry_after", 0)))
                retry_delay = min(retry_delay * 2, 30)
                continue
            retry_delay = 1
            now = int(time.time())
            self.store.prune(now, self.archive.retention)
            if updates:
                last_received = now
            for update in sorted(updates, key=lambda item: item.update_id):
                try:
                    await dispatcher.feed_update(self.bot, update, now=now)
                except TelegramAPIError as error:
                    LOG.warning("Telegram reply failed (%s)", type(error).__name__)
                offset = update.update_id + 1
            count, last = self.store.pending()
            if count and now >= notify_after:
                notify_after = now + 15
                try:
                    await self.send(self.screens.notification(count, last), silent=True)
                except TelegramAPIError as error:
                    notify_after = now + max(15, getattr(error, "retry_after", 0))
                    LOG.warning("Notification retry (%s)", type(error).__name__)
                else:
                    self.store.mark_notified(last)
