import express from "express";
import type { NextFunction, Request, Response } from "express";
import { getOctokit, parseRepo } from "./github/client.js";
import { createPullRequest, getCIStatus } from "./github/pr.js";
import {
  parseUrlVerification,
  parseFeishuEvent,
} from "./feishu/webhook.js";
import { sendTextMessage } from "./feishu/client.js";
import { startFeishuLongConnection } from "./feishu/longConnection.js";
import { runPipeline } from "./pipeline.js";
import type { FeishuEvent, ParsedRequirement } from "./feishu/types.js";

const app = express();

app.use("/feishu", (req: Request, _res: Response, next: NextFunction) => {
  console.log(
    `[Feishu] ${req.method} ${req.originalUrl} from ${req.ip} content-type=${req.headers["content-type"] ?? ""}`,
  );
  next();
});

app.use(express.json());
app.use(
  (err: unknown, req: Request, res: Response, next: NextFunction) => {
    if (err instanceof SyntaxError && req.path.startsWith("/feishu/")) {
      console.warn(`[Feishu] Invalid JSON body: ${err.message}`);
      res.status(400).json({ error: "invalid_json" });
      return;
    }

    next(err);
  },
);

const PORT = parseInt(process.env.TS_BRIDGE_PORT ?? "3001", 10);

// ─── GitHub endpoints ─────────────────────────────────────────────────────────

app.post("/github/create-pr", async (req: Request, res: Response) => {
  try {
    const octokit = getOctokit();
    const result = await createPullRequest(octokit, req.body);
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/github/ci-status", async (req: Request, res: Response) => {
  try {
    const octokit = getOctokit();
    const prNumber = parseInt(req.query.pr_number as string, 10);
    const repo = req.query.repo as string;
    const result = await getCIStatus(octokit, repo, prNumber);
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/github/close-pr", async (req: Request, res: Response) => {
  try {
    const octokit = getOctokit();
    const { pr_number, repo } = req.body as {
      pr_number: number;
      repo: string;
    };
    const { owner, repo: repoName } = parseRepo(repo);
    await octokit.pulls.update({
      owner,
      repo: repoName,
      pull_number: pr_number,
      state: "closed",
    });
    res.json({ ok: true });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ─── Feishu webhook: requirement → pipeline → reply ───────────────────────────

// Track in-flight pipelines to avoid duplicate runs
const running = new Set<string>();
const activeMessages = new Set<string>();
const recentMessages = new Set<string>();
const MESSAGE_DEDUP_TTL_MS = 10 * 60 * 1000;

export async function handleParsedRequirement(
  parsed: ParsedRequirement,
): Promise<void> {
  if (isDuplicateMessage(parsed.messageId)) {
    console.log(`[Feishu] Duplicate message ignored: ${parsed.messageId}`);
    return;
  }

  if (running.has(parsed.chatId)) {
    rememberMessage(parsed.messageId);
    await sendTextMessage(
      parsed.chatId,
      "上一个需求仍在处理中，请等待完成后再提交新需求。",
    );
    return;
  }

  running.add(parsed.chatId);
  activeMessages.add(parsed.messageId);
  console.log(
    `[Feishu] New requirement from ${parsed.senderOpenId}: ${parsed.text.slice(0, 80)}`,
  );

  try {
    // Parse optional repo= prefix from message
    let repo = process.env.PIPELINE_DEFAULT_REPO ?? "";
    let requirement = parsed.text;

    const repoMatch = requirement.match(/^repo[=:\s]+(\S+)\s*/);
    if (repoMatch) {
      repo = repoMatch[1];
      requirement = requirement.slice(repoMatch[0].length).trim();
    }

    if (!repo) {
      await sendTextMessage(
        parsed.chatId,
        [
          "未配置目标 GitHub 仓库。\n",
          "请设置 PIPELINE_DEFAULT_REPO 环境变量，",
          "或在消息中使用格式：repo=org/repo 需求描述",
        ].join(""),
      );
      return;
    }

    await sendTextMessage(
      parsed.chatId,
      `收到需求，正在处理...\n> ${requirement.slice(0, 100)}`,
    );

    const outcome = await runPipeline(requirement, repo);

    if (outcome.success && outcome.prUrl) {
      const ciEmoji = outcome.ciPassed ? "通过" : "未通过/超时";
      await sendTextMessage(
        parsed.chatId,
        [
          "PR 已创建",
          outcome.prUrl,
          `CI: ${outcome.ciPassed === undefined ? "未知" : ciEmoji}`,
          `Run: ${outcome.runId ?? "?"}`,
        ].join("\n"),
      );
    } else {
      await sendTextMessage(
        parsed.chatId,
        [
          "处理失败",
          `> ${(outcome.error ?? "未知错误").slice(0, 200)}`,
          outcome.runId ? `Run: ${outcome.runId}` : "",
        ].filter(Boolean).join("\n"),
      );
    }
  } catch (err: any) {
    console.error("[Feishu] Pipeline error:", err);
    await sendTextMessage(
      parsed.chatId,
      `系统异常: ${(err?.message ?? String(err)).slice(0, 200)}`,
    );
  } finally {
    running.delete(parsed.chatId);
    activeMessages.delete(parsed.messageId);
    rememberMessage(parsed.messageId);
  }
}

function isDuplicateMessage(messageId: string): boolean {
  return activeMessages.has(messageId) || recentMessages.has(messageId);
}

function rememberMessage(messageId: string): void {
  if (!messageId || recentMessages.has(messageId)) {
    return;
  }

  recentMessages.add(messageId);
  const timer = setTimeout(() => {
    recentMessages.delete(messageId);
  }, MESSAGE_DEDUP_TTL_MS);
  timer.unref?.();
}

app.post("/feishu/webhook", async (req: Request, res: Response) => {
  const body = req.body;

  // 1. URL verification handshake (Feishu event subscription)
  const challenge = parseUrlVerification(body);
  if (challenge) {
    res.json({ challenge });
    return;
  }

  // 2. Parse the requirement from the message
  const parsed = parseFeishuEvent(body as FeishuEvent);
  if (!parsed) {
    // Not a text message (image / file / sticker) — ignore silently
    res.json({ ok: true });
    return;
  }

  // 3. Acknowledge immediately — Feishu requires < 3 s response
  res.json({ ok: true });

  // 4. Kick off pipeline asynchronously
  void handleParsedRequirement(parsed);
});

app.all("/feishu/webhook", (req: Request, res: Response) => {
  res.status(405).json({
    error: "method_not_allowed",
    method: req.method,
  });
});

// ─── Health check ─────────────────────────────────────────────────────────────

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

app.listen(PORT, () => {
  console.log(`OpenClaw TS Bridge listening on http://localhost:${PORT}`);
});

startFeishuLongConnection(handleParsedRequirement);

export default app;
