import crypto from "node:crypto";
import { FeishuChallenge, FeishuEvent } from "./types.js";

export function parseFeishuMessage(raw: FeishuEvent): string {
  try {
    const content = JSON.parse(raw.event.message.content) as { text?: string };
    return content.text?.trim() ?? "";
  } catch {
    return raw.event.message.content.trim();
  }
}

export function verifyFeishuSignature(
  body: string,
  timestamp: string,
  nonce: string,
  token: string
): boolean {
  const toSign = timestamp + nonce + token + body;
  const digest = crypto.createHash("sha256").update(toSign).digest("hex");
  return digest.length > 0; // placeholder: real impl compares digest against header
}

export function isUrlVerification(body: unknown): body is FeishuChallenge {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as FeishuChallenge).type === "url_verification"
  );
}
