import express from "express";
import type { Request, Response } from "express";
import { getOctokit, parseRepo } from "./github/client.js";
import { createPullRequest, getCIStatus } from "./github/pr.js";
import {
  parseUrlVerification,
  parseFeishuEvent,
} from "./feishu/webhook.js";
import { sendTextMessage } from "./feishu/client.js";
import { runPipeline } from "./pipeline.js";
import type { FeishuEvent } from "./feishu/types.js";

const app = express();
app.use(express.json());

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

  // Dedup: one active pipeline per chat
  if (running.has(parsed.chatId)) {
    await sendTextMessage(
      parsed.chatId,
      "⏳ 上一个需求仍在处理中，请等待完成后再提交新需求。",
    );
    res.json({ ok: true });
    return;
  }

  // 3. Acknowledge immediately — Feishu requires < 3 s response
  res.json({ ok: true });

  // 4. Kick off pipeline asynchronously
  running.add(parsed.chatId);
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
          "❌ 未配置目标 GitHub 仓库。\n",
          "请设置 PIPELINE_DEFAULT_REPO 环境变量，",
          "或在消息中使用格式：`repo=org/repo 需求描述`",
        ].join(""),
      );
      return;
    }

    await sendTextMessage(
      parsed.chatId,
      `🤖 收到需求，正在处理...\n> ${requirement.slice(0, 100)}`,
    );

    // ── Run the pipeline ──────────────────────────────────────────────
    const outcome = await runPipeline(requirement, repo);

    if (outcome.success && outcome.prUrl) {
      const ciEmoji = outcome.ciPassed ? "✅" : "⚠️";
      await sendTextMessage(
        parsed.chatId,
        [
          `✅ PR 已创建\n`,
          `📎 ${outcome.prUrl}`,
          `🔬 CI: ${ciEmoji} ${outcome.ciPassed ? "通过" : outcome.ciPassed === false ? "未通过/超时" : "未知"}`,
          `🆔 Run: \`${outcome.runId ?? "?"}\``,
        ].join("\n"),
      );
    } else {
      await sendTextMessage(
        parsed.chatId,
        [
          "❌ 处理失败\n",
          `> ${(outcome.error ?? "未知错误").slice(0, 200)}`,
          outcome.runId ? `🆔 Run: \`${outcome.runId}\`` : "",
        ].filter(Boolean).join("\n"),
      );
    }
  } catch (err: any) {
    console.error(`[Feishu] Pipeline error:`, err);
    await sendTextMessage(
      parsed.chatId,
      `❌ 系统异常: ${(err?.message ?? String(err)).slice(0, 200)}`,
    );
  } finally {
    running.delete(parsed.chatId);
  }
});

// ─── Health check ─────────────────────────────────────────────────────────────

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

app.listen(PORT, () => {
  console.log(`OpenClaw TS Bridge listening on http://localhost:${PORT}`);
});

export default app;
