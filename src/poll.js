import { setTimeout as sleep } from 'node:timers/promises';
import { UPDATE_TYPES } from './journal.js';
import { TelegramError } from './telegram.js';

const SECOND = 1000;
const HOUR = 60 * 60;
const unixTime = () => Math.floor(Date.now() / SECOND);

export async function restoreConnection(telegram, journal) {
  const connectionId = journal.store.getSetting('connection');
  if (!connectionId) return;
  try {
    const connection = await telegram.call('getBusinessConnection', { business_connection_id: connectionId });
    journal.store.transaction(() => journal.setConnection(connection));
  } catch (error) {
    if (!(error instanceof TelegramError) || ![400, 403, 404].includes(error.code)) throw error;
    journal.store.transaction(() => journal.dropConnection());
  }
}

async function reply(telegram, ownerId, parts, signal, warn) {
  for (const part of parts) {
    const media = typeof part !== 'string';
    while (!signal.aborted) {
      try {
        if (media) await telegram.ownerMedia(ownerId, part);
        else await telegram.ownerMessage(ownerId, part);
        break;
      } catch (error) {
        if (signal.aborted) return;
        if (!(error instanceof TelegramError)) throw error;
        if (error.code === 429) {
          await sleep(Math.max(1, error.retryAfter) * SECOND, undefined, { signal });
          continue;
        }
        if (media && error.code === 400) {
          await telegram.ownerMessage(ownerId, 'Telegram не смог повторно отправить это вложение.');
        } else {
          warn(error.message);
        }
        break;
      }
    }
  }
}

export async function poll(telegram, journal, { signal, clock = unixTime, warn = console.warn }) {
  let offset;
  let lastReceived = clock();
  let retryDelay = 1;
  let notifyAfter = 0;

  while (!signal.aborted) {
    try {
      journal.store.prune(clock(), journal.retention);
      if (clock() - lastReceived >= HOUR) offset = undefined;
      const updates = await telegram.updates(offset, UPDATE_TYPES);
      if (signal.aborted) return;
      if (updates.length) lastReceived = clock();
      journal.store.prune(clock(), journal.retention);
      for (const update of updates.sort((a, b) => a.update_id - b.update_id)) {
        if (signal.aborted) return;
        const parts = journal.process(update, clock());
        offset = update.update_id + 1;
        if (parts) await reply(telegram, journal.ownerId, parts, signal, warn);
      }
      const { count, last } = journal.store.pending();
      if (count && clock() >= notifyAfter && !signal.aborted) {
        notifyAfter = clock() + 15;
        try {
          await telegram.ownerMessage(journal.ownerId,
            `Новые правки и удаления: ${count}. Открыть журнал: /archive\nПоследняя запись: /show ${last}`, true);
          journal.store.markNotified(last);
        } catch (error) {
          if (!(error instanceof TelegramError)) throw error;
          if (error.code === 429) notifyAfter = clock() + Math.max(15, error.retryAfter);
          if (!signal.aborted) warn(error.message);
        }
      }
      retryDelay = 1;
    } catch (error) {
      if (signal.aborted) return;
      if (!(error instanceof TelegramError) || !error.retryable) throw error;
      warn(error.message);
      const delay = Math.max(retryDelay, error.retryAfter);
      await sleep(delay * SECOND, undefined, { signal }).catch((waitError) => {
        if (!signal.aborted) throw waitError;
      });
      retryDelay = Math.min(retryDelay * 2, 30);
    }
  }
}
