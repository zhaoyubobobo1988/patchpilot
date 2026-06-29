/**
 * Feishu (Lark) Open API client — minimal surface for bot messaging.
 *
 * Uses Node.js 20+ built-in fetch — no extra dependencies.
 */
import type { TenantTokenResponse } from "./types.js";

const FEISHU_API = "https://open.feishu.cn/open-apis";

// ── Token cache ──────────────────────────────────────────────────────────────

let _cachedToken: string | null = null;
let _tokenExpiresAt: number = 0; // epoch ms

function appId(): string {
  return process.env.FEISHU_APP_ID ?? "";
}

function appSecret(): string {
  return process.env.FEISHU_APP_SECRET ?? "";
}

// ── Tenant access token ──────────────────────────────────────────────────────

export async function getTenantToken(): Promise<string> {
  // Reuse cached token with 60 s safety margin
  if (_cachedToken && Date.now() < _tokenExpiresAt - 60_000) {
    return _cachedToken;
  }

  const id = appId();
  const secret = appSecret();
  if (!id || !secret) {
    throw new Error("FEISHU_APP_ID and FEISHU_APP_SECRET must be set");
  }

  const res = await fetch(
    `${FEISHU_API}/auth/v3/tenant_access_token/internal`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ app_id: id, app_secret: secret }),
    },
  );

  const data: TenantTokenResponse = await res.json();
  if (data.code !== 0) {
    throw new Error(`Feishu token error: [${data.code}] ${data.msg}`);
  }

  _cachedToken = data.tenant_access_token;
  _tokenExpiresAt = Date.now() + data.expire * 1000;
  return _cachedToken;
}

// ── Send message to chat ─────────────────────────────────────────────────────

export async function sendTextMessage(
  chatId: string,
  text: string,
): Promise<void> {
  const token = await getTenantToken();

  const body = {
    receive_id: chatId,
    msg_type: "text",
    content: JSON.stringify({ text }),
  };

  const res = await fetch(
    `${FEISHU_API}/im/v1/messages?receive_id_type=chat_id`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );

  const data = await res.json();
  if (!res.ok || (data as any).code !== 0) {
    console.error(
      `[Feishu] sendMessage failed: HTTP ${res.status} —`,
      JSON.stringify(data).slice(0, 300),
    );
  }
}
