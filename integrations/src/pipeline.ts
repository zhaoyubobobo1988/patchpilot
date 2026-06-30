/**
 * Spawn the OpenClaw Python pipeline as a subprocess and return structured
 * result.  Uses child_process.spawn so long-running pipelines stream correctly.
 */
import { spawn } from "node:child_process";

export interface PipelineOutcome {
  success: boolean;
  runId?: string;
  prUrl?: string;
  ciPassed?: boolean;
  error?: string;
}

const PIPELINE_DIR = process.env.PIPELINE_DIR ?? "/opt/openclaw";
const PYTHON_BIN = process.env.PYTHON_BIN ?? "python";

/**
 * Run the pipeline with a natural-language requirement and target repo.
 *
 * Default repo can be set via ``PIPELINE_DEFAULT_REPO`` so Feishu users
 * only need to type the requirement itself.
 */
export function runPipeline(
  requirement: string,
  repo?: string,
): Promise<PipelineOutcome> {
  const repository =
    repo || process.env.PIPELINE_DEFAULT_REPO || "";

  if (!repository) {
    return Promise.resolve({
      success: false,
      error:
        "未配置目标仓库。请设置 PIPELINE_DEFAULT_REPO 环境变量，或在消息中指定 repo=org/repo",
    });
  }

  return new Promise((resolve) => {
    const args = [
      "pipeline.py",
      "--requirement", requirement,
      "--repo", repository,
    ];

    const child = spawn(PYTHON_BIN, args, {
      cwd: PIPELINE_DIR,
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf-8");
    });

    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf-8");
    });

    child.on("error", (err) => {
      resolve({ success: false, error: `无法启动 pipeline: ${err.message}` });
    });

    child.on("close", (code) => {
      const combined = stdout + stderr;

      // Extract structured fields from the final summary block
      const runId = normalizeField(firstMatch(combined, /Run ID\s*:\s*(\S+)/));
      const prUrl = normalizeField(firstMatch(combined, /PR URL\s*:\s*(.+)/));
      const ciPassed = normalizeField(firstMatch(combined, /CI Pass\s*:\s*(\S+)/));
      const errors = firstMatch(combined, /Errors\s*:\s*(.+)/);

      if (code === 0 && prUrl) {
        resolve({ success: true, runId, prUrl, ciPassed: ciPassed === "True" });
      } else if (code === 0) {
        resolve({
          success: false,
          runId,
          error: errors ?? "pipeline finished without a PR URL",
        });
      } else {
        const snippet = (stderr || stdout).slice(-800);
        resolve({
          success: false,
          runId,
          error: errors ?? snippet,
        });
      }
    });
  });
}

function firstMatch(text: string, re: RegExp): string | undefined {
  const m = text.match(re);
  return m?.[1]?.trim() || undefined;
}

function normalizeField(value: string | undefined): string | undefined {
  if (!value || value === "None" || value === "null" || value === "undefined") {
    return undefined;
  }
  return value;
}
