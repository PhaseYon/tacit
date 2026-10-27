import { cacheLife } from "next/cache";

import { REPO } from "./site";

// Public stats, cached and re-fetched in the background at most hourly (ISR).
// Every fetch is best-effort: a failure yields null and never fails the build.

export type GitHubStats = {
  stars: number | null;
  contributors: number | null;
  release: { tag: string; url: string } | null;
};

export type PyPIStats =
  | { status: "ok"; package: string; lastMonth: number }
  | { status: "soon"; package: string };

const TIMEOUT_MS = 6_000;

async function getJson(url: string, headers: Record<string, string> = {}) {
  const res = await fetch(url, {
    headers: { Accept: "application/json", "User-Agent": "tacit-site", ...headers },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  return res;
}

function githubHeaders(): Record<string, string> {
  // Optional: raises the GitHub API rate limit from 60/h. Read-only public data.
  const token = process.env.GITHUB_TOKEN;
  return {
    Accept: "application/vnd.github+json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export async function getGitHubStats(): Promise<GitHubStats> {
  "use cache";
  const out: GitHubStats = { stars: null, contributors: null, release: null };
  const h = githubHeaders();
  const api = `https://api.github.com/repos/${REPO}`;

  const [repo, contributors, release] = await Promise.allSettled([
    getJson(api, h),
    getJson(`${api}/contributors?per_page=1&anon=1`, h),
    getJson(`${api}/releases/latest`, h),
  ]);

  try {
    if (repo.status === "fulfilled" && repo.value.ok) {
      const body = (await repo.value.json()) as { stargazers_count?: number };
      out.stars = body.stargazers_count ?? null;
    }
  } catch {}

  try {
    if (contributors.status === "fulfilled" && contributors.value.ok) {
      // With per_page=1 the "last" page number is the contributor count.
      const link = contributors.value.headers.get("link") ?? "";
      const last = link.match(/[?&]page=(\d+)>;\s*rel="last"/);
      if (last) out.contributors = Number(last[1]);
      else out.contributors = ((await contributors.value.json()) as unknown[]).length;
    }
  } catch {}

  try {
    if (release.status === "fulfilled" && release.value.ok) {
      const body = (await release.value.json()) as { tag_name?: string; html_url?: string };
      if (body.tag_name && body.html_url) out.release = { tag: body.tag_name, url: body.html_url };
    }
  } catch {}

  // Retry sooner if GitHub didn't answer (rate limit, outage).
  if (out.stars === null) cacheLife("minutes");
  else cacheLife("hours");
  return out;
}

/**
 * Downloads from pypistats, but only if the PyPI project with this name is ours.
 * The name `tacit` is currently taken on PyPI by an unrelated project, so we check that
 * the project's URLs point at our repo before showing any number.
 */
export async function getPyPIStats(): Promise<PyPIStats> {
  "use cache";
  const pkg = process.env.PYPI_PACKAGE || "tacit";
  try {
    const meta = await getJson(`https://pypi.org/pypi/${encodeURIComponent(pkg)}/json`);
    if (!meta.ok) {
      cacheLife("hours");
      return { status: "soon", package: pkg };
    }
    const info = ((await meta.json()) as { info?: Record<string, unknown> }).info ?? {};
    const urls = [
      info.home_page,
      info.project_url,
      ...Object.values((info.project_urls as Record<string, string> | null) ?? {}),
    ];
    const ours = urls.some(
      (u) => typeof u === "string" && u.toLowerCase().includes(`github.com/${REPO.toLowerCase()}`),
    );
    if (!ours) {
      cacheLife("hours");
      return { status: "soon", package: pkg };
    }
    const res = await getJson(
      `https://pypistats.org/api/packages/${encodeURIComponent(pkg)}/recent`,
    );
    if (!res.ok) {
      cacheLife("minutes");
      return { status: "soon", package: pkg };
    }
    const body = (await res.json()) as { data?: { last_month?: number } };
    cacheLife("hours");
    if (typeof body.data?.last_month !== "number") return { status: "soon", package: pkg };
    return { status: "ok", package: pkg, lastMonth: body.data.last_month };
  } catch {
    cacheLife("minutes");
    return { status: "soon", package: pkg };
  }
}
