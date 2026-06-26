import express from "express";
import { getOctokit, parseRepo } from "./github/client.js";
import { createPullRequest, getCIStatus } from "./github/pr.js";
import { isUrlVerification, parseFeishuMessage } from "./feishu/webhook.js";

const app = express();
app.use(express.json());

const PORT = parseInt(process.env.TS_BRIDGE_PORT ?? "3001", 10);

// ─── GitHub endpoints ─────────────────────────────────────────────────────────

app.post("/github/create-pr", async (req, res) => {
  try {
    const octokit = getOctokit();
    const result = await createPullRequest(octokit, req.body);
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/github/ci-status", async (req, res) => {
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

app.post("/github/close-pr", async (req, res) => {
  try {
    const octokit = getOctokit();
    const { pr_number, repo } = req.body as { pr_number: number; repo: string };
    const { owner, repo: repoName } = parseRepo(repo);
    await octokit.pulls.update({ owner, repo: repoName, pull_number: pr_number, state: "closed" });
    res.json({ ok: true });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ─── 飞书 Webhook ─────────────────────────────────────────────────────────────

app.post("/feishu/webhook", (req, res) => {
  const body = req.body;

  // URL 验证握手
  if (isUrlVerification(body)) {
    res.json({ challenge: body.challenge });
    return;
  }

  const text = parseFeishuMessage(body);
  console.log(`[Feishu] Received requirement: ${text}`);

  // TODO: 触发 Python pipeline（MVP 阶段手动触发，后续集成）
  res.json({ ok: true });
});

// ─── Health check ─────────────────────────────────────────────────────────────

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.listen(PORT, () => {
  console.log(`OpenClaw TS Bridge listening on http://localhost:${PORT}`);
});

export default app;
