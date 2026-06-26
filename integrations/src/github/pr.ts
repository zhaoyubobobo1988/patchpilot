import { Octokit } from "@octokit/rest";
import { CICheckResult, CreatePRParams, PRCreatedResult } from "./types.js";
import { parseRepo } from "./client.js";

export async function createPullRequest(
  octokit: Octokit,
  params: CreatePRParams
): Promise<PRCreatedResult> {
  const { owner, repo } = parseRepo(params.repository);
  const { data } = await octokit.pulls.create({
    owner,
    repo,
    title: params.title,
    body: params.body,
    head: params.head_branch,
    base: params.base_branch,
    draft: params.draft ?? true,
  });
  return {
    number: data.number,
    html_url: data.html_url,
    head: { ref: data.head.ref },
    state: data.state,
  };
}

export async function getCIStatus(
  octokit: Octokit,
  repository: string,
  prNumber: number
): Promise<CICheckResult> {
  const { owner, repo } = parseRepo(repository);

  const { data: pr } = await octokit.pulls.get({ owner, repo, pull_number: prNumber });
  const sha = pr.head.sha;

  const { data: checks } = await octokit.checks.listForRef({ owner, repo, ref: sha });

  const runs = checks.check_runs;
  const total = runs.length;
  const completed = runs.filter((r) => r.status === "completed");
  const failed = completed.filter(
    (r) => r.conclusion !== "success" && r.conclusion !== "skipped"
  );

  if (total === 0) return { status: "pending", failedChecks: [] };
  if (completed.length < total) return { status: "running", failedChecks: [] };

  if (failed.length > 0) {
    return {
      status: "failure",
      failedChecks: failed.map((r) => ({ name: r.name, conclusion: r.conclusion ?? null })),
    };
  }
  return { status: "success", failedChecks: [] };
}

export async function downloadCILogs(
  octokit: Octokit,
  repository: string,
  runId: number
): Promise<string> {
  const { owner, repo } = parseRepo(repository);
  const resp = await octokit.actions.downloadWorkflowRunLogs({ owner, repo, run_id: runId });
  // resp.data is a binary zip — return URL for downstream processing
  return (resp as any).url ?? "";
}
