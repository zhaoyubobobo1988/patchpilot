/**
 * Feishu webhook helpers — URL verification + message parsing.
 */
import type {
  FeishuEvent,
  ParsedRequirement,
} from "./types.js";

// ── URL verification ─────────────────────────────────────────────────────────

export function parseUrlVerification(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;

  const payload = body as {
    type?: unknown;
    challenge?: unknown;
    header?: { event_type?: unknown };
    event?: { challenge?: unknown };
  };

  if (
    payload.type === "url_verification" &&
    typeof payload.challenge === "string"
  ) {
    return payload.challenge;
  }

  if (
    payload.header?.event_type === "url_verification" &&
    typeof payload.event?.challenge === "string"
  ) {
    return payload.event.challenge;
  }

  return null;
}

// ── Message parsing ──────────────────────────────────────────────────────────

/**
 * Extract a {@link ParsedRequirement} from an inbound Feishu event.
 * Returns `null` when the event is not a text message or the content is empty.
 */
export function parseFeishuEvent(
  body: FeishuEvent,
): ParsedRequirement | null {
  const ev = body?.event;
  if (!ev) return null;

  const { message, sender } = ev;
  if (!message || !sender) return null;
  if (message.message_type !== "text") return null;

  let text = "";
  try {
    const parsed = JSON.parse(message.content) as { text?: string };
    text = (parsed.text ?? "").trim();
  } catch {
    text = message.content.trim();
  }

  if (!text) return null;

  return {
    text,
    chatId: message.chat_id,
    messageId: message.message_id,
    senderOpenId: sender.sender_id.open_id,
  };
}
