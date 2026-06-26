import { Octokit } from "@octokit/rest";

let _octokit: Octokit | null = null;

export function getOctokit(): Octokit {
  if (!_octokit) {
    const token = process.env.GITHUB_TOKEN;
    if (!token) throw new Error("GITHUB_TOKEN environment variable is required");
    _octokit = new Octokit({ auth: token });
  }
  return _octokit;
}

export function parseRepo(repository: string): { owner: string; repo: string } {
  const [owner, repo] = repository.split("/");
  if (!owner || !repo) throw new Error(`Invalid repository format: ${repository}`);
  return { owner, repo };
}
