import { randomBytes } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { Journal } from './journal.js';
import { poll, restoreConnection } from './poll.js';
import { Store } from './store.js';
import { Telegram, TelegramError } from './telegram.js';

function getToken() {
  const token = process.env.TELEGRAM_BOT_TOKEN ?? '';
  if (!/^\d+:[A-Za-z0-9_-]+$/.test(token)) throw new Error('Укажи TELEGRAM_BOT_TOKEN в .env.');
  return token;
}

function readArchiveConfig() {
  const owner = process.env.TELEGRAM_OWNER_ID ?? '';
  const ownerId = Number(owner);
  if (!/^\d+$/.test(owner) || !Number.isSafeInteger(ownerId) || ownerId < 1) {
    throw new Error('Укажи числовой TELEGRAM_OWNER_ID в .env. Для определения ID есть npm run identify.');
  }
  const keyText = process.env.ARCHIVE_KEY_BASE64 ?? '';
  const key = Buffer.from(keyText, 'base64');
  if (key.length !== 32 || key.toString('base64') !== keyText) {
    throw new Error('ARCHIVE_KEY_BASE64 должен быть случайным 32-байтным ключом в base64.');
  }
  const days = Number(process.env.ARCHIVE_RETENTION_DAYS ?? 7);
  if (!Number.isInteger(days) || days < 1 || days > 90) {
    throw new Error('ARCHIVE_RETENTION_DAYS должен быть целым числом от 1 до 90.');
  }
  return { ownerId, key, retention: days * 86400, path: resolve(process.env.ARCHIVE_DB || './data/archive.sqlite') };
}

async function identify(telegram, bot, signal) {
  const command = `/start id_${randomBytes(12).toString('hex')}`;
  console.log(`Отправь в личный чат @${bot.username} ровно эту строку:\n${command}`);
  let offset;
  while (!signal.aborted) {
    const updates = await telegram.updates(offset, ['message']);
    for (const { update_id: id, message } of updates) {
      offset = id + 1;
      if (message?.chat?.type === 'private' && message.from?.id === message.chat.id && message.text === command) {
        console.log(`TELEGRAM_OWNER_ID=${message.from.id}`);
        return;
      }
    }
  }
}

async function main(signal) {
  const mode = process.argv[2];
  if (process.argv.length > 3 || (mode && mode !== 'identify')) throw new Error('Команды: npm start или npm run identify.');
  const telegram = new Telegram(getToken(), { signal });
  const config = mode ? null : readArchiveConfig();
  const bot = await telegram.call('getMe');
  const webhook = await telegram.call('getWebhookInfo');
  if (webhook.url) throw new Error('У бота настроен webhook. Для этого проекта используй отдельного бота с long polling.');
  if (mode === 'identify') return identify(telegram, bot, signal);
  if (!bot.can_connect_to_business) throw new Error('Включи Secretary Mode в настройках бота через @BotFather.');

  process.umask(0o077);
  mkdirSync(dirname(config.path), { recursive: true, mode: 0o700 });
  const store = new Store(config.path, config.key, { ownerId: config.ownerId, botId: bot.id });
  try {
    const journal = new Journal(store, { ...config, username: bot.username });
    store.prune(Math.floor(Date.now() / 1000), config.retention);
    await restoreConnection(telegram, journal);
    console.log(`Журнал запущен. Отправь /start в личный чат @${bot.username}.`);
    await poll(telegram, journal, { signal });
  } finally {
    store.close();
  }
}

const shutdown = new AbortController();
process.once('SIGINT', () => shutdown.abort());
process.once('SIGTERM', () => shutdown.abort());
try {
  await main(shutdown.signal);
} catch (error) {
  if (!shutdown.signal.aborted) {
    const message = error instanceof TelegramError && error.code === 409
      ? 'Telegram вернул 409. Убедись, что запущен один экземпляр бота и webhook не настроен.'
      : error.message;
    console.error(message);
    process.exitCode = 1;
  }
}
