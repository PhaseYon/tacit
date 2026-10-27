import { getPyPIStats } from "@/lib/stats";
import { REPO_URL } from "@/lib/site";
import { CodeLine } from "./copy-button";

// Until our package is on PyPI, `uvx tacit` would resolve to whatever owns that name
// there (currently an unrelated project). Show the install-from-GitHub form until then.
export async function InstallNote() {
  const pypi = await getPyPIStats();
  if (pypi.status === "ok") return null;
  return (
    <div className="mt-3 rounded-lg border border-warn-fg/30 bg-warn-soft p-3 text-sm text-warn-fg">
      <p>
        <strong className="font-semibold">Not on PyPI yet.</strong> Until the first release,
        install from GitHub:
      </p>
      <div className="mt-2 text-fg">
        <CodeLine code={`uvx --from git+${REPO_URL} tacit init`} />
      </div>
    </div>
  );
}
