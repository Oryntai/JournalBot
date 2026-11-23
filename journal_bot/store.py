import hashlib
import hmac
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from os import urandom

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DAY = 86_400
MAX_ID = 2**53 - 1
PAGE_SIZE = 8


@dataclass(frozen=True)
class Entry:
    id: int
    chat_key: str
    expires: int
    body: dict
    previous_id: int | None = None


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class Store:
    def __init__(self, path, master_key: bytes, owner_id: int, bot_id: int):
        def derive(purpose):
            return HKDF(algorithm=hashes.SHA256(), length=32,
                        salt=b"telegram-chat-journal/v1", info=purpose).derive(master_key)

        self.cipher = AESGCM(derive(b"encryption"))
        self.index_key = derive(b"indexes")
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript("""
                PRAGMA secure_delete = ON;
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS seen_updates (id INTEGER PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS seen_expiry ON seen_updates(expires);
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_key TEXT NOT NULL,
                    message_key TEXT NOT NULL, expires INTEGER NOT NULL,
                    pending INTEGER NOT NULL, body BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS journal_chat ON journal(chat_key, id);
                CREATE INDEX IF NOT EXISTS journal_message ON journal(message_key, id);
                CREATE INDEX IF NOT EXISTS journal_expiry ON journal(expires);
                CREATE INDEX IF NOT EXISTS journal_pending ON journal(id) WHERE pending = 1;
            """)
            identity = {"ownerId": owner_id, "botId": bot_id}
            with self.transaction():
                existing = self.setting("identity")
                if existing is None:
                    self.set_setting("identity", identity)
                elif existing != identity:
                    raise ValueError("База привязана к другому боту или владельцу.")
                self.prepare_filters()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def update(self, update_id, now):
        with self.transaction():
            inserted = self.db.execute("INSERT OR IGNORE INTO seen_updates VALUES (?, ?)",
                                       (update_id, now + 2 * DAY)).rowcount
            yield bool(inserted)

    def close(self):
        self.db.close()

    def seal(self, value, context):
        nonce = urandom(12)
        encrypted = self.cipher.encrypt(nonce, json_bytes(value), context.encode())
        # The Node.js archive put the GCM tag before the ciphertext.
        return nonce + encrypted[-16:] + encrypted[:-16]

    def unseal(self, body, context):
        try:
            return json.loads(self.cipher.decrypt(body[:12], body[28:] + body[12:28], context.encode()))
        except InvalidTag:
            raise ValueError("Ключ архива неверен или база повреждена.") from None

    def setting(self, name):
        row = self.db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return self.unseal(row["value"], f"setting:{name}") if row else None

    def set_setting(self, name, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                        (name, self.seal(value, f"setting:{name}")))

    def index(self, *parts):
        return hmac.new(self.index_key, json_bytes(parts), hashlib.sha256).hexdigest()

    def prepare_filters(self):
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(journal)")}
        if "kind_key" not in columns:
            self.db.execute("ALTER TABLE journal ADD COLUMN kind_key TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS journal_kind ON journal(kind_key, id)")
        while rows := self.db.execute("SELECT * FROM journal WHERE kind_key IS NULL LIMIT 128").fetchall():
            self.db.executemany("UPDATE journal SET kind_key = ? WHERE id = ?", (
                (self.index("kind", self.entry(row).body["kind"]), row["id"]) for row in rows
            ))

    def chat_key(self, connection_id, chat_id):
        return self.index("chat", connection_id, chat_id)[:16]

    def message_key(self, connection_id, chat_id, message_id):
        return self.index("message", connection_id, chat_id, message_id)

    def entry(self, row):
        if row is None:
            return None
        body = self.unseal(row["body"], f"message:{row['message_key']}:{row['expires']}")
        prev = row["previous_id"] if "previous_id" in row.keys() else None
        return Entry(row["id"], row["chat_key"], row["expires"], body, prev)

    def append(self, chat_key, message_key, expires, event):
        self.db.execute("""INSERT INTO journal (chat_key, message_key, expires, pending, body, kind_key)
                         VALUES (?, ?, ?, ?, ?, ?)""", (chat_key, message_key, expires, event["kind"] != "new",
                         self.seal(event, f"message:{message_key}:{expires}"), self.index("kind", event["kind"])))

    def latest(self, message_key):
        return self.entry(self.db.execute("SELECT * FROM journal WHERE message_key = ? ORDER BY id DESC LIMIT 1",
                                          (message_key,)).fetchone())

    def event(self, event_id):
        return self.entry(self.db.execute("""
            SELECT entry.*, (SELECT MAX(old.id) FROM journal AS old
                WHERE old.message_key = entry.message_key AND old.id < entry.id) AS previous_id
            FROM journal AS entry WHERE entry.id = ?
            """, (event_id,)).fetchone())

    def history(self, chat: str | None = None, before=MAX_ID, kind: str | None = None):
        filters, params = ["entry.id < ?"], [before]
        if chat:
            filters.append("entry.chat_key = ?")
            params.append(chat)
        if kind:
            filters.append("entry.kind_key = ?")
            params.append(self.index("kind", kind))
        rows = self.db.execute(f"""
            SELECT entry.* FROM journal AS entry WHERE {' AND '.join(filters)}
            AND NOT EXISTS (SELECT 1 FROM journal AS newer
                WHERE newer.message_key = entry.message_key AND newer.id > entry.id)
            ORDER BY entry.id DESC LIMIT ?
            """, (*params, PAGE_SIZE + 1))
        return [self.entry(row) for row in rows]

    def chats(self, before=MAX_ID):
        rows = self.db.execute("""
            SELECT * FROM journal WHERE id IN (SELECT MAX(id) FROM journal GROUP BY chat_key)
            AND id < ? ORDER BY id DESC LIMIT ?
            """, (before, PAGE_SIZE + 1))
        return [self.entry(row) for row in rows]

    def pending(self):
        row = self.db.execute("SELECT COUNT(*), MAX(id) FROM journal WHERE pending = 1").fetchone()
        return tuple(row)

    def mark_notified(self, through_id):
        self.db.execute("UPDATE journal SET pending = 0 WHERE pending = 1 AND id <= ?", (through_id,))

    def forget(self, message_key):
        self.db.execute("DELETE FROM journal WHERE message_key = ?", (message_key,))

    def clear(self):
        self.db.execute("DELETE FROM journal")

    def prune(self, now, retention):
        with self.transaction():
            self.db.execute("DELETE FROM journal WHERE expires <= ?", (now,))
            previous = self.setting("retention")
            if previous is not None and retention < previous:
                self.shorten_deadlines(now, retention)
            if previous != retention:
                self.set_setting("retention", retention)
            self.db.execute("DELETE FROM seen_updates WHERE expires <= ?", (now,))

    def shorten_deadlines(self, now, retention):
        after = 0
        while rows := self.db.execute("SELECT * FROM journal WHERE id > ? ORDER BY id LIMIT 128", (after,)).fetchall():
            for row in rows:
                event = self.entry(row).body
                deadline = min(row["expires"], event["messageDate"] + retention)
                if deadline <= now:
                    self.db.execute("DELETE FROM journal WHERE id = ?", (row["id"],))
                elif deadline < row["expires"]:
                    sealed = self.seal(event, f"message:{row['message_key']}:{deadline}")
                    self.db.execute("UPDATE journal SET expires = ?, body = ? WHERE id = ?", (deadline, sealed, row["id"]))
            after = rows[-1]["id"]
