import re
from dataclasses import dataclass
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .archive import MEDIA_LABELS, one_line
from .store import DAY, MAX_ID, PAGE_SIZE

ARCHIVE_BUTTON = "🗂 Архив"
CHATS_BUTTON = "💬 Чаты"
SETTINGS_BUTTON = "⚙️ Настройки"
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ARCHIVE_BUTTON, style="primary"), KeyboardButton(text=CHATS_BUTTON)],
              [KeyboardButton(text=SETTINGS_BUTTON)]],
    resize_keyboard=True, is_persistent=True,
)
STATUS = {"new": ("●", "Новое"), "edit": ("✏️", "Изменено"), "delete": ("🗑", "Удалено")}
FILTERS = {"all": ("Все", None), "del": ("Удалённые", "delete"), "edit": ("Правки", "edit")}
CHAT_KEY = re.compile(r"[a-f0-9]{16}\Z")


def id_value(value):
    if not re.fullmatch(r"[0-9]{1,16}", value):
        raise ValueError("Invalid record ID")
    result = int(value)
    if not 0 < result <= MAX_ID:
        raise ValueError("Invalid record ID")
    return result


def cursor_value(value):
    return MAX_ID if value == "0" else id_value(value)


@dataclass(frozen=True)
class Page:
    chat: str = "all"
    kind: str = "all"
    before: int = MAX_ID

    @classmethod
    def parse(cls, chat, kind, before):
        if (chat != "all" and not CHAT_KEY.fullmatch(chat)) or kind not in FILTERS:
            raise ValueError("Invalid archive filter")
        return cls(chat, kind, cursor_value(before))

    def action(self, entry_id=None):
        tail = f"{self.chat}:{self.kind}:{0 if self.before == MAX_ID else self.before}"
        return f"j:e:{entry_id}:{tail}" if entry_id else f"j:a:{tail}"


@dataclass
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None


def button(text, action, style=None):
    return InlineKeyboardButton(text=text, callback_data=action, style=style)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def current(entry):
    return entry.body["after"] or entry.body["before"]


class Screens:
    def __init__(self, store, retention, zone):
        self.store, self.retention, self.zone = store, retention, zone

    def date(self, at, pattern="%d.%m · %H:%M"):
        return datetime.fromtimestamp(at, self.zone).strftime(pattern)

    def start(self):
        if self.store.setting("connection"):
            hint = "Открой архив или выбери чат в меню."
        else:
            hint = "Подключи бота в Telegram → Настройки → Автоматизация чатов. Затем включи запись в меню бота."
        return Screen(f"<b>Журнал чатов</b>\n\n{hint}", MAIN_MENU)

    def archive(self, page=Page()):
        chat = None if page.chat == "all" else page.chat
        entries = self.store.history(chat, page.before, FILTERS[page.kind][1])
        page_entries = entries[:PAGE_SIZE]
        title = f"<b>{escape(current(entries[0])['peer'])}</b>" if chat and entries else "<b>Архив</b>"
        if page_entries:
            context = "Все личные чаты" if chat is None else "Сообщения чата"
            text = f"🗂 {title}\n{context}"
        elif page.kind == "all" and page.before == MAX_ID:
            text = f"🗂 {title}\nВ этом разделе пока пусто."
        else:
            text = f"🗂 {title}\nНа этой странице нет записей."
        rows = [[button(label, Page(page.chat, key).action(), "primary" if key == page.kind else None)
                 for key, (label, _) in FILTERS.items()]]
        for entry in page_entries:
            item = current(entry)
            icon, _ = STATUS[entry.body["kind"]]
            media = MEDIA_LABELS[item["media"]["type"]].capitalize() if item["media"] else "Сообщение"
            preview = one_line(item["text"], 26 if chat else 20) or media
            peer = f"{one_line(item['peer'], 11)} · " if chat is None else ""
            label = f"{icon} {self.date(entry.body['at'], '%H:%M')} · {peer}{preview}"
            rows.append([button(label, page.action(entry.id))])
        nav = []
        if page.before != MAX_ID:
            nav.append(button("« К началу", Page(page.chat, page.kind).action()))
        if len(entries) > PAGE_SIZE:
            nav.append(button("Ещё ›", Page(page.chat, page.kind, page_entries[-1].id).action()))
        if nav:
            rows.append(nav)
        rows.append([button("💬 Выбрать чат", "j:c:0")])
        return Screen(text, keyboard(rows))

    def chats(self, before=MAX_ID):
        entries = self.store.chats(before)
        text = "💬 <b>Чаты</b>\nВыбери чат, чтобы открыть его архив." if entries else "💬 <b>Чаты</b>\nСохранённых чатов пока нет."
        rows = [[button(current(entry)["peer"], Page(entry.chat_key).action())] for entry in entries[:PAGE_SIZE]]
        nav = []
        if before != MAX_ID:
            nav.append(button("« К началу", "j:c:0"))
        if len(entries) > PAGE_SIZE:
            nav.append(button("Ещё ›", f"j:c:{entries[PAGE_SIZE - 1].id}"))
        if nav:
            rows.append(nav)
        rows.append([button("Все чаты", Page().action())])
        return Screen(text, keyboard(rows))

    def entry(self, entry_id, page=Page()):
        entry = self.store.event(entry_id)
        back = [button("‹ Назад в архив", page.action())]
        if entry is None:
            return Screen("Эта запись больше недоступна.", keyboard([back]))
        icon, status = STATUS[entry.body["kind"]]
        text = [f"{icon} <b>{status}</b> · {self.date(entry.body['at'])}",
                escape(current(entry)["peer"])]
        sides = (("before", "Было"), ("after", "Стало")) if entry.body["kind"] == "edit" else (
            (("after", ""),) if entry.body["after"] else (("before", ""),))
        rows = []
        for side, title in sides:
            value = entry.body[side]
            if title:
                text.append(f"\n<b>{title}</b>")
            if value is None:
                text.append("Версия отсутствует в архиве.")
                continue
            content = value["text"]
            if content:
                excerpt = content if len(content) <= 850 else content[:849] + "…"
                text.append(f"<blockquote>{escape(excerpt)}</blockquote>")
            actions = []
            if value["media"]:
                kind = value["media"]["type"]
                label = "🖼 Открыть фото" if kind == "photo" else f"📎 {MEDIA_LABELS[kind].capitalize()}"
                actions.append(button(f"{label} · {title.lower()}" if title else label,
                                      f"j:m:{entry.id}:{side}", "primary"))
            if len(content) > 850:
                actions.append(button("Весь текст", f"j:t:{entry.id}:{side}"))
            if actions:
                rows.append(actions)
        if entry.previous_id:
            rows.append([button("Предыдущая версия", page.action(entry.previous_id))])
        rows.append(back)
        return Screen("\n".join(text), keyboard(rows))

    def settings(self, notice=None):
        connected = bool(self.store.setting("connection"))
        active = self.store.setting("enabledAt") is not None
        lines = ["⚙️ <b>Настройки</b>", "", "🟢 Запись включена" if active else "⚪️ Запись на паузе",
                 f"Срок хранения · {self.retention // DAY} дн."]
        if not connected:
            lines += ["", "Подключи бота через Telegram → Настройки → Автоматизация чатов."]
        if notice:
            lines += ["", escape(notice)]
        rows = []
        if connected:
            rows.append([button("Поставить на паузу", "j:pause") if active
                         else button("Включить запись", "j:enable", "success")])
        rows += [[button("Как хранятся данные", "j:privacy")], [button("Очистить архив", "j:clear", "danger")]]
        return Screen("\n".join(lines), keyboard(rows))

    def confirm_clear(self):
        return Screen("<b>Очистить архив?</b>\nСохранённые записи удалятся с сервера, запись остановится.",
                      keyboard([[button("Удалить записи", "j:purge", "danger"), button("Отмена", "j:settings")]]))

    def privacy(self):
        text = ("<b>Что хранит бот</b>\n\n"
                f"Текст, названия чатов, версии сообщений и идентификаторы обычных вложений хранятся до {self.retention // DAY} дн. "
                "Архив зашифрован и доступен владельцу. Сами файлы остаются в Telegram.\n\n"
                "Записи, которые ты открыл в личке с ботом, можно удалить средствами Telegram.")
        return Screen(text, keyboard([[button("‹ Настройки", "j:settings")]]))

    def notification(self, count, last):
        return Screen(f"В архиве новые правки и удаления · <b>{count}</b>", keyboard([[
            button("Последняя запись", Page().action(last), "primary"), button("Архив", Page().action()),
        ]]))
