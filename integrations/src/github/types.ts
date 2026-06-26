export interface CreatePRParams {
  repository: string;   // "org/repo"
  title: string;
  body: string;
  head_branch: string;
  base_branch: string;
  draft?: boolean;
}

export interface PRCreatedResult {
  number: number;
  html_url: string;
  head: { ref: string };
  state: string;
}

export interface CICheckResult {
  status: "pending" | "running" | "success" | "failure";
  failedChecks: Array<{ name: string; conclusion: string | null }>;
  logsUrl?: string;
  rawLog?: string;
}
