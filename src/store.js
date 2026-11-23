import { createCipheriv, createDecipheriv, createHmac, hkdfSync, randomBytes } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

const TWO_DAYS = 2 * 24 * 60 * 60;

export class Store {
  constructor(path, masterKey, identity) {
    const derive = (purpose) => Buffer.from(hkdfSync(
      'sha256', masterKey, Buffer.from('telegram-chat-journal/v1'), Buffer.from(purpose), 32,
    ));
    this.encryptionKey = derive('encryption');
    this.indexKey = derive('indexes');
    this.db = new DatabaseSync(path);
    try {
      this.db.exec(`
        PRAGMA busy_timeout = 5000;
        PRAGMA secure_delete = ON;

        CREATE TABLE IF NOT EXISTS settings (
          name TEXT PRIMARY KEY, value BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_updates (
          id INTEGER PRIMARY KEY, expires INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS seen_expiry ON seen_updates(expires);
        CREATE TABLE IF NOT EXISTS journal (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_key TEXT NOT NULL,
          message_key TEXT NOT NULL,
          expires INTEGER NOT NULL,
          pending INTEGER NOT NULL,
          body BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS journal_chat ON journal(chat_key, id);
        CREATE INDEX IF NOT EXISTS journal_message ON journal(message_key, id);
        CREATE INDEX IF NOT EXISTS journal_expiry ON journal(expires);
        CREATE INDEX IF NOT EXISTS journal_pending ON journal(id) WHERE pending = 1;
      `);
      this.transaction(() => {
        const bound = this.getSetting('identity');
        if (bound === null) this.setSetting('identity', identity);
        else if (bound.ownerId !== identity.ownerId || bound.botId !== identity.botId) {
          throw new Error('База привязана к другому боту или владельцу.');
        }
      });
    } catch (error) {
      this.db.close();
      throw error;
    }
  }

  close() {
    this.db.close();
  }

  transaction(operation) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = operation();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  once(updateId, now, operation) {
    return this.transaction(() => {
      const result = this.db.prepare('INSERT OR IGNORE INTO seen_updates VALUES (?, ?)')
        .run(updateId, now + TWO_DAYS);
      if (result.changes === 0) return null;
      return operation();
    });
  }

  seal(value, context) {
    const nonce = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.encryptionKey, nonce);
    cipher.setAAD(Buffer.from(context));
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]);
    return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]);
  }

  unseal(body, context) {
    const bytes = Buffer.from(body);
    const decipher = createDecipheriv('aes-256-gcm', this.encryptionKey, bytes.subarray(0, 12));
    decipher.setAAD(Buffer.from(context));
    decipher.setAuthTag(bytes.subarray(12, 28));
    let plaintext;
    try {
      plaintext = Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]);
    } catch {
      throw new Error('Ключ архива неверен или база повреждена.');
    }
    return JSON.parse(plaintext.toString('utf8'));
  }

  getSetting(name) {
    const row = this.db.prepare('SELECT value FROM settings WHERE name = ?').get(name);
    return row ? this.unseal(row.value, `setting:${name}`) : null;
  }

  setSetting(name, value) {
    this.db.prepare('INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)')
      .run(name, this.seal(value, `setting:${name}`));
  }

  chatKey(connectionId, chatId) {
    return this.index(['chat', connectionId, chatId]).slice(0, 16);
  }

  messageKey(connectionId, chatId, messageId) {
    return this.index(['message', connectionId, chatId, messageId]);
  }

  index(parts) {
    return createHmac('sha256', this.indexKey).update(JSON.stringify(parts)).digest('hex');
  }

  unpack(row) {
    if (!row) return null;
    return {
      id: row.id,
      chatKey: row.chat_key,
      expires: row.expires,
      ...this.unseal(row.body, `message:${row.message_key}:${row.expires}`),
    };
  }

  append(chatKey, messageKey, expires, event) {
    this.db.prepare('INSERT INTO journal (chat_key, message_key, expires, pending, body) VALUES (?, ?, ?, ?, ?)')
      .run(chatKey, messageKey, expires, Number(event.kind !== 'new'),
        this.seal(event, `message:${messageKey}:${expires}`));
  }

  latest(messageKey) {
    return this.unpack(this.db.prepare('SELECT * FROM journal WHERE message_key = ? ORDER BY id DESC LIMIT 1')
      .get(messageKey));
  }

  event(id) {
    const row = this.db.prepare(`
      SELECT entry.*, (
        SELECT MAX(previous.id) FROM journal AS previous
        WHERE previous.message_key = entry.message_key AND previous.id < entry.id
      ) AS previous_id
      FROM journal AS entry WHERE entry.id = ?
    `).get(id);
    return row ? { ...this.unpack(row), previousId: row.previous_id } : null;
  }

  history(chatKey, before) {
    const chatFilter = chatKey ? 'AND entry.chat_key = ?' : '';
    const parameters = chatKey ? [before, chatKey] : [before];
    return this.db.prepare(`
      SELECT entry.* FROM journal AS entry
      WHERE entry.id < ? ${chatFilter}
      AND NOT EXISTS (
        SELECT 1 FROM journal AS newer
        WHERE newer.message_key = entry.message_key AND newer.id > entry.id
      )
      ORDER BY entry.id DESC LIMIT 11
    `).all(...parameters).map((row) => this.unpack(row));
  }

  chats(before) {
    return this.db.prepare(`
      SELECT * FROM journal WHERE id IN (SELECT MAX(id) FROM journal GROUP BY chat_key)
      AND id < ? ORDER BY id DESC LIMIT 21
    `).all(before).map((row) => this.unpack(row));
  }

  pending() {
    return this.db.prepare(`
      SELECT COUNT(*) AS count, MAX(id) AS last FROM journal WHERE pending = 1
    `).get();
  }

  markNotified(throughId) {
    this.db.prepare('UPDATE journal SET pending = 0 WHERE pending = 1 AND id <= ?').run(throughId);
  }

  forgetMessage(messageKey) {
    this.db.prepare('DELETE FROM journal WHERE message_key = ?').run(messageKey);
  }

  clear() {
    this.db.exec('DELETE FROM journal');
  }

  prune(now, retention) {
    this.transaction(() => {
      this.db.prepare('DELETE FROM journal WHERE expires <= ?').run(now);
      const retentionBeforeEdit = this.getSetting('retention');
      if (retentionBeforeEdit !== null && retention < retentionBeforeEdit) {
        // Tighten existing deadlines without extending them when configuration changes.
        const remaining = this.db.prepare('SELECT id, message_key, expires, body FROM journal').all();
        const update = this.db.prepare('UPDATE journal SET expires = ?, body = ? WHERE id = ?');
        const remove = this.db.prepare('DELETE FROM journal WHERE id = ?');
        for (const row of remaining) {
          const event = this.unpack(row);
          const deadline = Math.min(row.expires, event.messageDate + retention);
          if (deadline <= now) remove.run(row.id);
          else if (deadline < row.expires) {
            update.run(deadline, this.seal(eventData(event), `message:${row.message_key}:${deadline}`), row.id);
          }
        }
      }
      if (retentionBeforeEdit !== retention) this.setSetting('retention', retention);
      this.db.prepare('DELETE FROM seen_updates WHERE expires <= ?').run(now);
    });
  }
}

function eventData({ id, chatKey, expires, ...event }) {
  return event;
}
