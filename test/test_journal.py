import base64
import unittest
from datetime import timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from journal_bot.bot import JournalBot
from journal_bot.screens import Page
from journal_bot.store import DAY, MAX_ID, PAGE_SIZE, Store

OWNER, BOT, PEER = 11001, 12001, 22001
NOW = 1_796_000_000
KEY = b"\x13" * 32
CONNECTION = "fixture-connection"


def user(user_id):
    return {"id": user_id, "is_bot": user_id == BOT, "first_name": "Тестовый чат"}


def chat(user_id):
    return {"id": user_id, "type": "private", "first_name": "Тестовый чат"}


class TelegramStub(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:", KEY, OWNER, BOT)
        self.session = TelegramStub()
        self.bot = Bot(f"{BOT}:mock_bot_token", session=self.session)
        self.journal = JournalBot(self.bot, self.store, OWNER, 7 * DAY, timezone.utc, "TestJournalBot")
        self.dispatcher = Dispatcher(disable_fsm=True)
        self.dispatcher.include_router(self.journal.router)
        self.number = 0

    async def asyncTearDown(self):
        self.store.close()
        await self.session.close()

    async def update(self, key, data, *, offset=0, update_id=None):
        self.number += 1
        update = Update.model_validate({"update_id": update_id or self.number, key: data}, context={"bot": self.bot})
        await self.dispatcher.feed_update(self.bot, update, now=NOW + offset)

    async def say(self, text, user_id=OWNER):
        await self.update("message", {"message_id": 1, "date": NOW, "from": user(user_id), "chat": chat(user_id), "text": text})

    async def click(self, data, user_id=OWNER, chat_id=OWNER, offset=0):
        await self.update("callback_query", {"id": str(self.number), "chat_instance": "test", "data": data,
                          "from": user(user_id), "message": {"message_id": 3, "date": NOW, "from": user(BOT),
                          "chat": chat(chat_id), "text": "Экран архива"}}, offset=offset)

    async def connect(self):
        await self.update("business_connection", {"id": CONNECTION, "user": user(OWNER), "user_chat_id": OWNER,
                          "date": NOW, "is_enabled": True})
        await self.click("j:enable")

    async def record(self, text="текст", message_id=42, key="business_message", update_id=None, **fields):
        details = {"business_connection_id": CONNECTION, "message_id": message_id, "date": NOW + 1,
                   "from": user(PEER), "chat": chat(PEER), "text": text, **fields}
        await self.update(key, details, offset=2, update_id=update_id)

    async def delete(self):
        await self.update("deleted_business_messages", {"business_connection_id": CONNECTION, "chat": chat(PEER),
                          "message_ids": [42, 42]}, offset=3)

    async def test_menu_to_deleted_photo_through_real_router(self):
        await self.say("/start")
        menu = self.session.calls[-1]
        self.assertIsInstance(menu.reply_markup, ReplyKeyboardMarkup)
        self.assertIn("Архив", menu.reply_markup.keyboard[0][0].text)
        await self.connect()
        photo = {"file_id": "AgAC_ordinary_file", "file_unique_id": "fixture-photo", "width": 320, "height": 240}
        await self.record(None, photo=[photo])
        await self.delete()
        await self.say("/show")
        listing = self.session.calls[-1]
        self.assertIsInstance(listing.reply_markup, InlineKeyboardMarkup)
        buttons = [item for row in listing.reply_markup.inline_keyboard for item in row if item.callback_data.startswith("j:e:")]
        self.assertEqual(len(buttons), 1)
        await self.click(buttons[0].callback_data)
        detail = self.session.calls[-1]
        self.assertIn("Удалено", detail.text)
        rows = detail.reply_markup.inline_keyboard
        self.assertEqual(rows[0][0].text, "🖼 Открыть фото")
        self.assertIn("Предыдущая версия", rows[1][0].text)
        self.assertIn("Назад", rows[-1][0].text)
        await self.click(rows[0][0].callback_data)
        sent = self.session.calls[-1]
        self.assertEqual(sent.__api_method__, "sendPhoto")
        self.assertEqual(sent.photo, photo["file_id"])
        self.assertEqual(sent.chat_id, OWNER)
        self.assertTrue(sent.protect_content)

    async def test_edit_filter_uses_latest_status_and_escapes_markup(self):
        await self.connect()
        await self.record("<b>& old")
        edit_id = 12345
        await self.record("new", key="edited_business_message", update_id=edit_id, edit_date=NOW + 2)
        await self.record("new", key="edited_business_message", update_id=edit_id, edit_date=NOW + 2)
        edits = self.store.history(kind="edit")
        self.assertEqual(len(edits), 1)
        rendered = self.journal.screens.entry(edits[0].id).text
        self.assertIn("&lt;b&gt;&amp; old", rendered)
        await self.delete()
        self.assertEqual(self.store.history(kind="edit"), [])
        self.assertEqual(len(self.store.history(kind="delete")), 1)
        self.assertEqual(self.store.pending()[0], 2)

    async def test_foreign_user_and_chat_cannot_open_or_purge(self):
        await self.connect()
        await self.record("private")
        count = len(self.session.calls)
        await self.say("/archive", PEER)
        await self.click("j:e:1:all:all:0", user_id=PEER)
        await self.click("j:purge", chat_id=PEER)
        self.assertEqual(len(self.session.calls), count)
        self.assertEqual(len(self.store.history()), 1)
        await self.click("j:clear")
        self.assertEqual(len(self.store.history()), 1)
        await self.click("j:purge")
        self.assertEqual(self.store.history(), [])
        self.assertIsNone(self.store.setting("enabledAt"))

    async def test_retention_and_protected_media(self):
        await self.connect()
        self.store.prune(NOW, 7 * DAY)
        await self.record("secret", has_protected_content=True, message_id=99)
        await self.record(None, photo=[{"file_id": "Hidden", "file_unique_id": "hidden", "width": 1, "height": 1, "ttl_seconds": 1}])
        self.assertEqual(self.store.history(), [])
        await self.record("visible")
        await self.delete()
        self.store.prune(NOW + DAY + 1, DAY)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.pending()[0], 0)

    async def test_page_cursor_does_not_resurface_old_versions(self):
        await self.connect()
        for index in range(1, PAGE_SIZE + 2):
            await self.record("same text", message_id=index)
        await self.record("new", message_id=1, key="edited_business_message", edit_date=NOW + 2)
        first = self.store.history()
        next_page = self.store.history(before=first[PAGE_SIZE - 1].id)
        self.assertEqual(len(first), PAGE_SIZE + 1)
        self.assertEqual([entry.id for entry in next_page], [2])
        self.assertLessEqual(len(Page("f" * 16, "edit", MAX_ID - 1).action(MAX_ID).encode()), 64)


class CompatibilityTests(unittest.TestCase):
    def test_legacy_node_ciphertext_and_index_backfill(self):
        store = Store(":memory:", KEY, OWNER, BOT)
        self.addCleanup(store.close)
        key = store.message_key(CONNECTION, PEER, 42)
        self.assertEqual(key, "6b9776cc452e2ca5bfa897681e224786b578e7705d23eafc41fdb022ebaf2fae")
        self.assertEqual(store.chat_key(CONNECTION, PEER), "d1506b9ebbd81aca")
        # Ciphertext from v0.1.1 with a synthetic photo, before the kind_key column was populated.
        old = base64.b64decode(
            "o9uL+GeIVJLLETk52KgdadsyMfADe40Og+V9BqUiwoGiKvaU6MX44/WbWVjGqmBUdpgacJ+dx6yAxIbpc2b27"
            "Rex/gfSo2FAOkG3DpjZHEd6fP7Vcjn/a+SBwb2Vft3rmYtKwTFhH1bIP0oqt7BwnBXzDhS4DHGuLY+7WqfuZUkP"
            "BNR0P8e2m0cIIvgP9FeQ1Gl8xS7etKtQedNsc0bQYEqgZxA3WY4emwlkACv2VZbbUZCr7hP56zeAyfmZhx0Ob"
            "X4nGUbQDW9IHQKdi/Z8ZdnAItTjNd61SBpyxvLtmQr1n8LM4FFauTWsLEiRY3c8hzFRpvgkkbTAL1LR31T9UWY"
            "t011gLN8Nrg49aaCSWfx/"
        )
        store.db.execute("INSERT INTO journal (chat_key, message_key, expires, pending, body) VALUES (?, ?, ?, 0, ?)",
                         (store.chat_key(CONNECTION, PEER), key, NOW, old))
        with store.transaction():
            store.prepare_filters()
        [entry] = store.history(kind="delete")
        self.assertEqual(entry.body["before"]["media"]["id"], "AgAC_example")
