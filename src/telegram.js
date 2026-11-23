const LONG_POLL_SECONDS = 20;

export class TelegramError extends Error {
  constructor(method, code, retryAfter = 0) {
    super(`Telegram ${method}: ${code || 'нет ответа'}`);
    this.code = code;
    this.retryAfter = retryAfter;
  }

  get retryable() {
    return this.code === 0 || this.code === 429 || this.code >= 500;
  }
}

export class Telegram {
  constructor(token, { signal, transport = fetch } = {}) {
    this.url = `https://api.telegram.org/bot${token}/`;
    this.signal = signal;
    this.transport = transport;
  }

  async call(method, parameters = {}) {
    const timeout = AbortSignal.timeout((method === 'getUpdates' ? 35 : 15) * 1000);
    const signal = this.signal ? AbortSignal.any([this.signal, timeout]) : timeout;
    let response;
    let payload;
    try {
      response = await this.transport(`${this.url}${method}`, {
        method: 'POST', redirect: 'error', signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parameters),
      });
      payload = await response.json();
    } catch {
      throw new TelegramError(method, response && !response.ok ? response.status : 0);
    }
    if (!response.ok || payload.ok !== true) {
      throw new TelegramError(method, payload.error_code ?? response.status, payload.parameters?.retry_after);
    }
    return payload.result;
  }

  updates(offset, allowed) {
    return this.call('getUpdates', { offset, allowed_updates: allowed, timeout: LONG_POLL_SECONDS });
  }

  ownerMessage(ownerId, text, silent = false) {
    return this.call('sendMessage', {
      chat_id: ownerId, text, protect_content: true, disable_notification: silent,
      link_preview_options: { is_disabled: true },
    });
  }

  ownerMedia(ownerId, { method, parameters }) {
    return this.call(method, { ...parameters, chat_id: ownerId, protect_content: true });
  }
}
