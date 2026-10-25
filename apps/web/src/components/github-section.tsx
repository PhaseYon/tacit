import { headers } from "next/headers";

import { ConnectGitHubButton, LinkGitHubButton } from "@/components/github-buttons";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/auth";

type Repo = {
  id: string;
  full_name: string;
  url: string | null;
  visibility: string;
  backfill_status: string;
  acl_synced_at: string | null;
  can_read: boolean;
  documents: number;
};
type Status = { configured: boolean; installations: string[]; repos: Repo[] };

const ERRORS: Record<string, string> = {
  expired: "The connection link expired. Please try again.",
  not_linked: "Link your GitHub account first.",
  not_allowed:
    "That installation belongs to a GitHub account or org you don't administer, so it wasn't connected.",
  missing_installation: "GitHub didn't send an installation. Please try again.",
  failed: "Something went wrong connecting GitHub.",
};

export async function GitHubSection({
  workspaceId,
  slug,
  canManage,
  flash,
}: {
  workspaceId: string;
  slug: string;
  canManage: boolean;
  flash: { connected?: boolean; requested?: boolean; error?: string };
}) {
  const [status, accounts] = await Promise.all([
    apiFetch<Status>(`/workspaces/${workspaceId}/github`),
    auth.api.listUserAccounts({ headers: await headers() }),
  ]);
  const linked = accounts.some((a) => a.providerId === "github");

  return (
    <Card>
      <CardHeader>
        <CardTitle>GitHub</CardTitle>
        <CardDescription>
          PRs, issues, reviews and docs. You only see repos you can access on GitHub.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {flash.connected && (
          <p className="text-sm text-emerald-600 dark:text-emerald-400">
            GitHub connected. Repos will appear as they sync.
          </p>
        )}
        {flash.requested && (
          <p className="text-sm text-muted-foreground">
            Install requested. An org admin needs to approve it on GitHub.
          </p>
        )}
        {flash.error && (
          <p className="text-sm text-destructive">{ERRORS[flash.error] ?? ERRORS.failed}</p>
        )}

        {!status.configured ? (
          <p className="text-sm text-muted-foreground">
            The GitHub App isn&apos;t configured on this server yet (see infra/github/README.md).
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {!linked && <LinkGitHubButton slug={slug} />}
            {canManage && linked && <ConnectGitHubButton workspaceId={workspaceId} slug={slug} />}
          </div>
        )}
        {!linked && status.repos.length > 0 && (
          <p className="text-sm text-muted-foreground">
            Link your GitHub account to see content from private repos you have access to.
          </p>
        )}

        {status.repos.length > 0 && (
          <ul className="flex flex-col divide-y rounded-lg border">
            {status.repos.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                <a href={r.url ?? "#"} className="font-medium hover:underline">
                  {r.full_name}
                </a>
                <Badge variant="secondary">{r.visibility}</Badge>
                {r.backfill_status !== "done" && <Badge variant="outline">{r.backfill_status}</Badge>}
                <span className="ml-auto text-muted-foreground">
                  {r.can_read ? `${r.documents} documents` : "no access"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
