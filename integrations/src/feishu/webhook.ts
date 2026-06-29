/**
 * Feishu webhook helpers — URL verification + message parsing.
 */
import type {
  FeishuChallenge,
  FeishuEvent,
  ParsedRequirement,
} from "./types.js";

// ── URL verification ─────────────────────────────────────────────────────────

export function isUrlVerification(
  body: unknown,
): body is FeishuChallenge {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as FeishuChallenge).type === "url_verification"
  );
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
