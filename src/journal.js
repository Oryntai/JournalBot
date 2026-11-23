const EVENT_LABELS = { new: 'Новое', edit: 'Изменено', delete: 'Удалено' };
const MEDIA = {
  animation: ['sendAnimation', 'анимация'],
  photo: ['sendPhoto', 'фото'],
  video: ['sendVideo', 'видео'],
  voice: ['sendVoice', 'голосовое'],
  video_note: ['sendVideoNote', 'видеосообщение'],
  audio: ['sendAudio', 'аудио'],
  document: ['sendDocument', 'файл'],
  sticker: ['sendSticker', 'стикер'],
};

export const UPDATE_TYPES = [
  'message', 'business_connection', 'business_message', 'edited_business_message',
  'deleted_business_messages',
];

function singleLine(text, length) {
  const chars = Array.from(text.replace(/\s+/gu, ' ').trim());
  return chars.length <= length ? chars.join('') : `${chars.slice(0, length - 1).join('')}…`;
}

function positiveInteger(number) {
  return Number.isSafeInteger(number) && number > 0;
}

function textParts(text) {
  const chars = Array.from(text);
  const parts = [];
  for (let i = 0; i < chars.length; i += 1800) parts.push(chars.slice(i, i + 1800).join(''));
  return parts;
}

function formatDate(unixSeconds) {
  return new Date(unixSeconds * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
}

function restrictsArchiving(message) {
  return Boolean(message.has_protected_content || message.chat.has_protected_content
    || message.ephemeral_message_id || message.is_paid_post || message.paid_media
    || message.ttl_seconds || message.self_destruct_type || message.is_view_once);
}

function snapshot(message, ownerId) {
  const text = message.text ?? message.caption ?? '';
  const name = [message.chat.first_name, message.chat.last_name].filter(Boolean).join(' ');
  const peer = singleLine(name || message.chat.username || 'Личный чат', 60);
  let media = null;
  for (const type of Object.keys(MEDIA)) {
    const file = type === 'photo' ? message.photo?.at(-1) : message[type];
    if (!file || !/^[A-Za-z0-9_-]+$/.test(file.file_id ?? '')) continue;
    if (file.ttl_seconds || file.self_destruct_type || file.is_view_once) return null;
    media = {
      type, id: file.file_id, uniqueId: file.file_unique_id ?? file.file_id,
      spoiler: message.has_media_spoiler === true,
    };
    break;
  }
  if (!text && !media) return null;
  return { peer, text, media, outgoing: message.from?.id === ownerId };
}

function sameContent(before, after) {
  return before !== null && before.text === after.text && before.media?.type === after.media?.type
    && before.media?.uniqueId === after.media?.uniqueId && before.media?.spoiler === after.media?.spoiler;
}

function preview(event) {
  const { text, media } = event.after ?? event.before;
  return singleLine(text || `[${MEDIA[media.type][1]}]`, 85);
}

function summary(event) {
  const { peer, outgoing } = event.after ?? event.before;
  return `#${event.id} · ${EVENT_LABELS[event.kind]} · ${formatDate(event.at)}\n`
    + `${outgoing ? '→' : '←'} ${peer} · ${preview(event)}`;
}

function displaySnapshot(title, value) {
  if (!value) return [`${title}\nВерсия отсутствует в архиве.`];
  const parts = textParts(`${title}\n${value.text || `[${MEDIA[value.media.type][1]}]`}`);
  if (value.media) {
    const { type, id, spoiler } = value.media;
    const parameters = { [type]: id };
    if (spoiler && ['photo', 'video', 'animation'].includes(type)) parameters.has_spoiler = true;
    parts.push({ method: MEDIA[type][0], parameters });
  }
  return parts;
}

export class Journal {
  constructor(store, { ownerId, username, retention }) {
    this.store = store;
    this.ownerId = ownerId;
    this.username = username.toLowerCase();
    this.retention = retention;
  }

  setConnection(connection) {
    if (connection.user?.id !== this.ownerId) return;
    const current = this.store.getSetting('connection');
    if (!connection.is_enabled) {
      if (current === connection.id) this.dropConnection();
      return;
    }
    if (current !== connection.id) {
      this.dropConnection();
      this.store.setSetting('connection', connection.id);
    }
  }

  dropConnection() {
    this.store.clear();
    this.store.setSetting('connection', null);
    this.store.setSetting('enabledAt', null);
  }

  process(update, now) {
    return this.store.once(update.update_id, now, () => {
      if (update.business_connection) this.setConnection(update.business_connection);
      if (update.business_message) this.record(update.business_message, 'new', now);
      if (update.edited_business_message) this.record(update.edited_business_message, 'edit', now);
      if (update.deleted_business_messages) this.recordDeletes(update.deleted_business_messages, now);
      if (update.message) return this.command(update.message, now);
      return null;
    });
  }

  captures(connectionId, chat) {
    return typeof connectionId === 'string' && connectionId === this.store.getSetting('connection')
      && this.store.getSetting('enabledAt') !== null
      && chat?.type === 'private' && positiveInteger(chat.id);
  }

  record(message, kind, now) {
    const connectionId = message.business_connection_id;
    if (!this.captures(connectionId, message.chat) || !positiveInteger(message.message_id)) return;
    const messageKey = this.store.messageKey(connectionId, message.chat.id, message.message_id);
    if (restrictsArchiving(message)) {
      this.store.forgetMessage(messageKey);
      return;
    }
    if (!positiveInteger(message.date) || message.date < this.store.getSetting('enabledAt')) return;
    const after = snapshot(message, this.ownerId);
    if (!after) return;

    const previous = this.store.latest(messageKey);
    const expires = previous?.expires ?? message.date + this.retention;
    if (expires <= now || previous?.kind === 'delete') return;
    if (kind === 'new' && previous) return;
    const at = message.edit_date ?? message.date;
    if (previous && at < previous.at) return;
    const before = previous?.after ?? null;
    if (sameContent(before, after)) return;
    const chatKey = this.store.chatKey(connectionId, message.chat.id);
    this.store.append(chatKey, messageKey, expires, {
      kind, before, after, at, messageDate: message.date,
    });
  }

  recordDeletes(update, now) {
    const connectionId = update.business_connection_id;
    if (!this.captures(connectionId, update.chat)) return;
    const chatKey = this.store.chatKey(connectionId, update.chat.id);
    for (const id of update.message_ids) {
      const key = this.store.messageKey(connectionId, update.chat.id, id);
      const previous = this.store.latest(key);
      if (!previous?.after || previous.expires <= now) continue;
      this.store.append(chatKey, key, previous.expires, {
        kind: 'delete', before: previous.after, after: null, at: now, messageDate: previous.messageDate,
      });
    }
  }

  command(message, now) {
    if (message.business_connection_id || message.chat?.type !== 'private'
        || message.chat.id !== this.ownerId || message.from?.id !== this.ownerId) return null;
    const [head, ...args] = (message.text ?? '').trim().split(/\s+/u);
    const [command, target] = head.split('@');
    if (target && target.toLowerCase() !== this.username) return null;
    switch (command) {
      case '/start':
      case '/help':
        return [
          'Журнал чатов\nПодключи бота в Telegram → Настройки → Автоматизация чатов и выбери нужные личные чаты.\n\n'
          + `/enable — начать сохранять новые сообщения на ${this.retention / 86400} дн.\n`
          + '/disable — поставить запись на паузу\n/status — состояние\n'
          + '/chats — чаты в архиве\n/archive — последняя страница архива\n'
          + '/show ID — открыть запись по номеру\n/purge confirm — очистить архив и остановить запись\n'
          + '/privacy — какие данные сохраняет бот',
        ];
      case '/status': {
        const connected = this.store.getSetting('connection') !== null;
        const enabled = this.store.getSetting('enabledAt') !== null;
        return [`Автоматизация ${connected ? 'подключена' : 'отключена'}. Запись ${enabled ? 'включена' : 'на паузе'}.`];
      }
      case '/privacy':
        return ['После /enable бот шифрует и хранит текст, ссылки Telegram на обычные вложения, '
          + `названия чатов и события правки и удаления до ${this.retention / 86400} дн. `
          + 'Архив доступен владельцу в этом диалоге. /purge confirm очищает базу бота. '
          + 'Сообщения, уже открытые в этом диалоге, можно удалить в Telegram.'];
      case '/enable':
        if (!this.store.getSetting('connection')) return ['Сначала подключи бота в разделе «Автоматизация чатов».'];
        if (this.store.getSetting('enabledAt') === null) this.store.setSetting('enabledAt', now);
        return [`Запись включена. Срок хранения — ${this.retention / 86400} дн. с даты исходного сообщения.`];
      case '/disable':
        this.store.setSetting('enabledAt', null);
        return ['Запись на паузе. Сохранённое можно открыть через /archive или удалить через /purge confirm.'];
      case '/purge':
        if (args.join(' ') !== 'confirm') return ['Для очистки архива отправь /purge confirm.'];
        this.store.clear();
        this.store.setSetting('enabledAt', null);
        return ['Архив очищен. Запись на паузе.'];
      case '/chats':
        return this.listChats(args);
      case '/archive':
        return this.listHistory(args);
      case '/show':
        return this.show(args);
      default:
        return null;
    }
  }

  listChats([cursor, ...rest]) {
    const before = cursor === undefined ? Number.MAX_SAFE_INTEGER : Number(cursor);
    if (rest.length || !positiveInteger(before)) return ['Формат команды: /chats [до ID].'];
    const page = this.store.chats(before);
    if (!page.length) return ['В этой части архива пока нет чатов.'];
    const shown = page.slice(0, 20);
    const rows = shown.map((event) => `${(event.after ?? event.before).peer} · ${event.chatKey}`);
    rows.push('\nОткрыть чат: /archive КОД');
    if (page.length > 20) rows.push(`Ещё чаты: /chats ${shown.at(-1).id}`);
    return [rows.join('\n')];
  }

  listHistory([chat = 'all', cursor, ...rest]) {
    const before = cursor === undefined ? Number.MAX_SAFE_INTEGER : Number(cursor);
    if (rest.length || !/^(all|[a-f0-9]{16})$/u.test(chat) || !positiveInteger(before)) {
      return ['Формат команды: /archive [all или код чата] [до ID]. Коды чатов — в /chats.'];
    }
    const page = this.store.history(chat === 'all' ? null : chat, before);
    if (!page.length) return ['В этой части архива пока нет записей.'];
    const shown = page.slice(0, 10);
    const rows = shown.map(summary);
    rows.push('Открыть запись: /show ID');
    if (page.length > 10) rows.push(`Следующая страница: /archive ${chat} ${shown.at(-1).id}`);
    return [rows.join('\n\n')];
  }

  show([id, ...rest]) {
    if (rest.length || !positiveInteger(Number(id))) return ['Формат команды: /show ID. Номера записей — в /archive.'];
    const event = this.store.event(Number(id));
    if (!event) return ['Запись отсутствует в архиве.'];
    let header = `#${event.id} · ${EVENT_LABELS[event.kind]} · ${formatDate(event.at)}\n${(event.after ?? event.before).peer}`;
    if (event.previousId !== null) header += `\nПредыдущая запись: /show ${event.previousId}`;
    if (event.kind === 'edit') {
      return [header, ...displaySnapshot('Было', event.before), ...displaySnapshot('Стало', event.after)];
    }
    return [header, ...displaySnapshot('Сообщение', event.after ?? event.before)];
  }
}
