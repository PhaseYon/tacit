import { getGitHubStats, getPyPIStats } from "@/lib/stats";
import { REPO_URL } from "@/lib/site";

const fmt = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

function Stat({ label, value, href }: { label: string; value: string; href?: string }) {
  const inner = (
    <>
      <span className="font-mono text-sm font-semibold text-fg tabular-nums group-hover:text-accent">
        {value}
      </span>
      <span className="text-xs text-muted">{label}</span>
    </>
  );
  return (
    <li>
      {href ? (
        <a href={href} className="group flex flex-col items-start">
          {inner}
        </a>
      ) : (
        <div className="flex flex-col items-start">{inner}</div>
      )}
    </li>
  );
}

export async function LiveStats() {
  const [gh, pypi] = await Promise.all([getGitHubStats(), getPyPIStats()]);
  return (
    <ul className="flex flex-wrap gap-x-8 gap-y-3" aria-label="Project stats">
      <Stat
        label="GitHub stars"
        value={gh.stars === null ? "—" : fmt.format(gh.stars)}
        href={`${REPO_URL}/stargazers`}
      />
      <Stat
        label="Contributors"
        value={gh.contributors === null ? "—" : fmt.format(gh.contributors)}
        href={`${REPO_URL}/graphs/contributors`}
      />
      <Stat
        label="Latest release"
        value={gh.release?.tag ?? "none yet"}
        href={gh.release?.url ?? `${REPO_URL}/releases`}
      />
      <Stat
        label="PyPI downloads / month"
        value={pypi.status === "ok" ? fmt.format(pypi.lastMonth) : "soon"}
        href={pypi.status === "ok" ? `https://pypistats.org/packages/${pypi.package}` : undefined}
      />
    </ul>
  );
}

export function LiveStatsFallback() {
  return (
    <div className="flex flex-wrap gap-x-8 gap-y-3" aria-hidden>
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex flex-col gap-1">
          <div className="h-4 w-10 animate-pulse rounded bg-surface-2" />
          <div className="h-3 w-20 animate-pulse rounded bg-surface-2" />
        </div>
      ))}
    </div>
  );
}
