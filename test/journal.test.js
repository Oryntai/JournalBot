import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { Journal, UPDATE_TYPES } from '../src/journal.js';
import { poll, restoreConnection } from '../src/poll.js';
import { Store } from '../src/store.js';
import { Telegram } from '../src/telegram.js';

const ownerId = 11001;
const botId = 12001;
const sentAt = 1_796_000_000;
const retention = 7 * 86400;
const key = Buffer.alloc(32, 0x13);
const connectionId = 'example-business-connection';
const peer = { id: 22001, type: 'private', first_name: 'Тестовый контакт' };
const anotherPeer = { id: 22002, type: 'private', first_name: 'Другой контакт' };
const business = (fields = {}) => ({ id: connectionId, user: { id: ownerId }, user_chat_id: ownerId,
  date: sentAt, is_enabled: true, ...fields });
const message = (fields = {}) => ({ business_connection_id: connectionId, chat: peer,
  from: { id: peer.id }, message_id: 42, date: sentAt + 2, text: 'Исходное сообщение', ...fields });
const deleted = (fields = {}) => ({ business_connection_id: connectionId, chat: peer, message_ids: [42], ...fields });
const command = (text, fields = {}) => ({ chat: { id: ownerId, type: 'private' },
  from: { id: ownerId }, text, ...fields });

function setup(t, active = true) {
  const store = new Store(':memory:', key, { ownerId, botId });
  t.after(() => store.close());
  const journal = new Journal(store, { ownerId, retention, username: 'TestArchiveBot' });
  if (active) {
    journal.process({ update_id: 1, business_connection: business() }, sentAt);
    journal.process({ update_id: 2, message: command('/enable') }, sentAt + 1);
  }
  return { store, journal };
}

test('правки и удаления с предыдущим текстом, дубликатами и одинаковыми ID в разных чатах', (t) => {
  const { store, journal } = setup(t);
  const edit = { update_id: 5, edited_business_message: message({ text: 'Новый текст', edit_date: sentAt + 4 }) };
  journal.process({ update_id: 3, business_message: message() }, sentAt + 2);
  journal.process({ update_id: 4, business_message: message({ chat: anotherPeer, text: 'Из другого чата' }) }, sentAt + 2);
  journal.process(edit, sentAt + 4);
  journal.process(edit, sentAt + 4);
  journal.process({ ...edit, update_id: 6 }, sentAt + 4);
  journal.process({ update_id: 7, deleted_business_messages: deleted({ message_ids: [42, 999, 42] }) }, sentAt + 5);

  const chatKey = store.chatKey(connectionId, peer.id);
  const entries = store.history(chatKey, Number.MAX_SAFE_INTEGER);
  assert.deepEqual(entries.map((entry) => entry.kind), ['delete']);
  const deletion = store.event(entries[0].id);
  const revision = store.event(deletion.previousId);
  const original = store.event(revision.previousId);
  assert.equal(original.kind, 'new');
  assert.equal(revision.kind, 'edit');
  assert.equal(revision.before.text, 'Исходное сообщение');
  assert.equal(revision.after.text, 'Новый текст');
  assert.equal(deletion.before.text, 'Новый текст');
  assert.deepEqual(store.pending(), { __proto__: null, count: 2, last: deletion.id });
  const [archive] = journal.process({ update_id: 8, message: command(`/archive ${chatKey}`) }, sentAt + 6);
  assert.match(archive, /Удалено/u);
  assert.doesNotMatch(archive, /Изменено|Новое|Из другого чата/u);
  const detail = journal.process({ update_id: 9, message: command(`/show ${deletion.id}`) }, sentAt + 6).join('\n');
  assert(detail.includes(`/show ${revision.id}`));
  assert.match(journal.process({ update_id: 10, message: command(`/show ${revision.id}`) }, sentAt + 6).join('\n'), /Было\nИсходное сообщение\nСтало\nНовый текст/u);
});

test('при листании архива старые версии скрыты, одинаковый текст в разных сообщениях сохраняется', (t) => {
  const { store, journal } = setup(t);
  for (let id = 1; id <= 11; id += 1) {
    journal.process({ update_id: id + 2, business_message: message({ message_id: id }) }, sentAt + 2);
  }
  journal.process({ update_id: 14, edited_business_message: message({ message_id: 1, text: 'Правка', edit_date: sentAt + 3 }) }, sentAt + 3);
  const chatKey = store.chatKey(connectionId, peer.id);
  for (const filter of [null, chatKey]) {
    const firstPage = store.history(filter, Number.MAX_SAFE_INTEGER);
    assert.deepEqual(firstPage.map((entry) => entry.id), [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2]);
    const secondPage = store.history(filter, firstPage[9].id);
    assert.deepEqual(secondPage.map((entry) => entry.id), [2]);
  }
});

test('запись только после включения и доступ только владельцу в личке', (t) => {
  const { store, journal } = setup(t, false);
  journal.process({ update_id: 1, business_connection: business({ user: { id: 13001 } }) }, sentAt);
  assert.match(journal.process({ update_id: 2, message: command('/enable') }, sentAt)[0], /Сначала/);
  journal.process({ update_id: 3, business_connection: business() }, sentAt);
  journal.process({ update_id: 4, business_message: message() }, sentAt + 2);
  journal.process({ update_id: 5, message: command('/enable') }, sentAt + 3);
  journal.process({ update_id: 6, business_message: message() }, sentAt + 4);
  journal.process({ update_id: 7, business_message: message({ date: sentAt + 4, business_connection_id: 'other' }) }, sentAt + 4);
  journal.process({ update_id: 8, business_message: message({ date: sentAt + 4, chat: { ...peer, type: 'group' } }) }, sentAt + 4);
  assert.equal(store.event(1), null);
  journal.process({ update_id: 9, business_message: message({ date: sentAt + 4 }) }, sentAt + 4);
  assert.equal(journal.process({ update_id: 10, message: command('/show 1', { from: { id: 13001 } }) }, sentAt + 5), null);
  assert.equal(journal.process({ update_id: 11, message: command('/show 1', { chat: { id: ownerId, type: 'group' } }) }, sentAt + 5), null);
  assert.match(journal.process({ update_id: 12, message: command('/show@TestArchiveBot 1') }, sentAt + 5).join('\n'), /Исходное/);
  journal.process({ update_id: 13, business_connection: business({ is_enabled: false }) }, sentAt + 6);
  assert.equal(store.event(1), null);
  assert.equal(store.getSetting('enabledAt'), null);
});

test('обычные вложения открываются по file_id, защищённые и одноразовые пропускаются', (t) => {
  const { store, journal } = setup(t);
  const photo = { file_id: 'AgAC_file', file_unique_id: 'unique', height: 640, width: 480 };
  journal.process({ update_id: 3, business_message: message({ text: undefined, caption: 'Фото', photo: [photo] }) }, sentAt + 2);
  journal.process({ update_id: 4, deleted_business_messages: deleted() }, sentAt + 3);
  const reply = journal.process({ update_id: 5, message: command('/show 2') }, sentAt + 4);
  assert.deepEqual(reply.at(-1), { method: 'sendPhoto', parameters: { photo: 'AgAC_file' } });
  journal.process({ update_id: 6, business_message: message({ message_id: 43, has_protected_content: true }) }, sentAt + 4);
  journal.process({ update_id: 7, business_message: message({ message_id: 44, text: undefined,
    caption: 'Одноразовое фото', photo: [{ ...photo, ttl_seconds: 1 }] }) }, sentAt + 4);
  assert.equal(store.event(3), null);
});

test('зашифрованный файл переживает перезапуск и проверяет ключ и владельца', (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'chat-journal-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, 'archive.sqlite');
  const identity = { ownerId, botId };
  let store = new Store(file, key, identity);
  const journal = new Journal(store, { ownerId, retention, username: 'TestArchiveBot' });
  journal.process({ update_id: 1, business_connection: business() }, sentAt);
  journal.process({ update_id: 2, message: command('/enable') }, sentAt + 1);
  journal.process({ update_id: 3, business_message: message() }, sentAt + 2);
  store.close();
  const bytes = readFileSync(file);
  assert.equal(bytes.includes('Исходное сообщение'), false);
  assert.equal(bytes.includes(connectionId), false);
  assert.throws(() => new Store(file, Buffer.alloc(32, 0x42), identity), /Ключ архива/u);
  assert.throws(() => new Store(file, key, { ...identity, ownerId: 777 }), /привязана/u);
  store = new Store(file, key, identity);
  t.after(() => store.close());
  assert.equal(store.event(1).after.text, 'Исходное сообщение');
  assert.equal(store.once(3, sentAt + 3, () => assert.fail('Повторное применение апдейта')), null);
});

test('TTL считается от исходного сообщения, а сокращение срока применяется к сохранённым записям', (t) => {
  const { store, journal } = setup(t);
  store.prune(sentAt + 1, retention);
  journal.process({ update_id: 3, business_message: message() }, sentAt + 2);
  journal.process({ update_id: 4, deleted_business_messages: deleted() }, sentAt + 3000);
  store.prune(sentAt + 4500, 5000);
  assert.equal(store.event(2).expires, sentAt + 5002);
  store.prune(sentAt + 5002, 5000);
  assert.equal(store.event(1), null);
  assert.equal(store.event(2), null);
  assert.equal(store.pending().count, 0);
});

test('пауза и явная очистка, включая ожидающие уведомления', (t) => {
  const { store, journal } = setup(t);
  journal.process({ update_id: 3, business_message: message() }, sentAt + 2);
  journal.process({ update_id: 4, deleted_business_messages: deleted() }, sentAt + 3);
  journal.process({ update_id: 5, message: command('/disable') }, sentAt + 3);
  journal.process({ update_id: 6, business_message: message({ message_id: 43, date: sentAt + 4 }) }, sentAt + 4);
  journal.process({ update_id: 7, message: command('/purge') }, sentAt + 5);
  assert.equal(store.event(3), null);
  assert.equal(store.pending().count, 1);
  journal.process({ update_id: 8, message: command('/purge confirm') }, sentAt + 6);
  assert.equal(store.event(1), null);
  assert.equal(store.pending().count, 0);
  assert.equal(store.getSetting('enabledAt'), null);
});

test('polling проходит весь сценарий HTTP и отправляет защищённые ответы только владельцу', async (t) => {
  const { store, journal } = setup(t, false);
  const stop = new AbortController();
  const requests = [];
  const updates = [
    { update_id: 5, deleted_business_messages: deleted() },
    { update_id: 1, business_connection: business() },
    { update_id: 2, message: command('/enable') },
    { update_id: 3, business_message: message({ date: sentAt + 9 }) },
    { update_id: 4, edited_business_message: message({ date: sentAt + 9, text: 'После правки' }) },
    { update_id: 6, message: command('/show 3') },
  ];
  const telegram = new Telegram('mock', { signal: stop.signal, transport: async (url, request) => {
    const method = url.split('/').at(-1);
    const params = JSON.parse(request.body);
    requests.push({ method, params });
    if (method === 'getUpdates' && params.offset !== undefined) stop.abort();
    return Response.json({ ok: true, result: method === 'getUpdates' ? updates : {} });
  } });
  await poll(telegram, journal, { signal: stop.signal, clock: () => sentAt + 9 });
  assert.deepEqual(requests[0].params.allowed_updates, UPDATE_TYPES);
  assert.equal(requests.at(-1).params.offset, 7);
  const sends = requests.filter((request) => request.method === 'sendMessage').map((request) => request.params);
  assert.equal(sends.length, 4);
  assert(sends.every((item) => item.chat_id === ownerId && item.protect_content && !item.business_connection_id));
  assert.match(sends[2].text, /После правки/u);
  assert.match(sends[3].text, /Новые правки и удаления: 2/u);
  assert.doesNotMatch(sends[3].text, /После правки/u);
  assert.equal(store.pending().count, 0);
});

test('проверка отозванного доступа при перезапуске удаляет архив', async (t) => {
  const { store, journal } = setup(t);
  journal.process({ update_id: 3, business_message: message() }, sentAt + 2);
  const telegram = new Telegram('mock', { transport: async () => Response.json({
    ok: false, error_code: 400,
  }, { status: 400 }) });
  await restoreConnection(telegram, journal);
  assert.equal(store.getSetting('connection'), null);
  assert.equal(store.event(1), null);
});
