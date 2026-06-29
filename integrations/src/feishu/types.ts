// ── Feishu inbound webhook event ──────────────────────────────────────────────

export interface FeishuEventHeader {
  event_id: string;
  event_type: string;
  token: string;
}

export interface FeishuSender {
  sender_id: {
    open_id: string;
    union_id?: string;
  };
}

export interface FeishuMessage {
  message_id: string;
  chat_id: string;
  chat_type: "group" | "p2p";
  message_type: string;
  content: string; // JSON-encoded string, e.g. '{"text":"hello"}'
}

export interface FeishuEvent {
  schema: string;
  header: FeishuEventHeader;
  event: {
    sender: FeishuSender;
    message: FeishuMessage;
  };
}

export interface FeishuChallenge {
  challenge: string;
  token: string;
  type: "url_verification";
}

// ── Feishu API responses ──────────────────────────────────────────────────────

export interface TenantTokenResponse {
  code: number;
  msg: string;
  tenant_access_token: string;
  expire: number; // seconds until expiry
}

// ── Parsed requirement from user message ──────────────────────────────────────

export interface ParsedRequirement {
  text: string; // the requirement description
  chatId: string;
  messageId: string;
  senderOpenId: string;
}
