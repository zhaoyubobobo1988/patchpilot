import * as Lark from "@larksuiteoapi/node-sdk";
import { parseFeishuEvent } from "./webhook.js";
import type { FeishuEvent, ParsedRequirement } from "./types.js";

type RequirementHandler = (parsed: ParsedRequirement) => Promise<void>;

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);

export function startFeishuLongConnection(handler: RequirementHandler): void {
  if (!TRUE_VALUES.has((process.env.FEISHU_LONG_CONNECTION_ENABLED ?? "").toLowerCase())) {
    return;
  }

  const appId = process.env.FEISHU_APP_ID ?? "";
  const appSecret = process.env.FEISHU_APP_SECRET ?? "";

  if (!appId || !appSecret) {
    console.warn(
      "[Feishu] Long connection disabled: FEISHU_APP_ID and FEISHU_APP_SECRET must be set",
    );
    return;
  }

  const eventDispatcher = new Lark.EventDispatcher({
    encryptKey: process.env.FEISHU_ENCRYPT_KEY || undefined,
    loggerLevel: Lark.LoggerLevel.info,
  }).register({
    "im.message.receive_v1": async (data: any) => {
      const parsed = parseFeishuEvent(toFeishuEvent(data));
      if (!parsed) {
        return;
      }

      await handler(parsed);
    },
  });

  const wsClient = new Lark.WSClient({
    appId,
    appSecret,
    loggerLevel: Lark.LoggerLevel.info,
  });

  wsClient.start({ eventDispatcher });
  console.log("[Feishu] Long connection started");
}

function toFeishuEvent(data: any): FeishuEvent {
  return {
    schema: "2.0",
    header: {
      event_id: String(data?.event_id ?? data?.uuid ?? ""),
      event_type: String(data?.event_type ?? "im.message.receive_v1"),
      token: String(data?.token ?? ""),
    },
    event: {
      sender: data?.sender,
      message: data?.message,
    },
  };
}
