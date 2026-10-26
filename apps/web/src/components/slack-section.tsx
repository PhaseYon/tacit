import { headers } from "next/headers";

import { ConnectSlackButton, LinkSlackButton } from "@/components/slack-buttons";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/auth";

type Channel = {
  id: string;
  name: string;
  url: string | null;
  is_private: boolean;
  backfill_status: string;
  acl_synced_at: string | null;
  can_read: boolean;
  documents: number;
};
type Status = { configured: boolean; teams: string[]; channels: Channel[] };

const ERRORS: Record<string, string> = {
  expired: "The connection link expired. Please try again.",
  cancelled: "The Slack install was cancelled.",
  not_allowed:
    "The Slack account that approved the install isn't the one you linked, so it wasn't connected.",
  taken: "That Slack workspace is already connected to another Tacit workspace.",
  failed: "Something went wrong connecting Slack.",
};

export async function SlackSection({
  workspaceId,
  slug,
  canManage,
  flash,
}: {
  workspaceId: string;
  slug: string;
  canManage: boolean;
  flash: { connected?: boolean; error?: string };
}) {
  const [status, accounts] = await Promise.all([
    apiFetch<Status>(`/workspaces/${workspaceId}/slack`),
    auth.api.listUserAccounts({ headers: await headers() }),
  ]);
  const linked = accounts.some((a) => a.providerId === "slack");

  return (
    <Card>
      <CardHeader>
        <CardTitle>Slack</CardTitle>
        <CardDescription>
          Messages and threads from channels you invite @Tacit to. No DMs. You only see channels
          your Slack account can read.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {flash.connected && (
          <p className="text-sm text-emerald-600 dark:text-emerald-400">
            Slack connected. Invite @Tacit to the channels you want synced.
          </p>
        )}
        {flash.error && (
          <p className="text-sm text-destructive">{ERRORS[flash.error] ?? ERRORS.failed}</p>
        )}

        {!status.configured ? (
          <p className="text-sm text-muted-foreground">
            The Slack app isn&apos;t configured on this server yet (see infra/slack/README.md).
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {!linked && <LinkSlackButton slug={slug} />}
            {canManage && linked && <ConnectSlackButton workspaceId={workspaceId} slug={slug} />}
          </div>
        )}
        {status.teams.length > 0 && (
          <p className="text-sm text-muted-foreground">Connected: {status.teams.join(", ")}</p>
        )}
        {!linked && status.channels.length > 0 && (
          <p className="text-sm text-muted-foreground">
            Link your Slack account to see messages from the {status.channels.length} synced
            channels.
          </p>
        )}

        {status.channels.length > 0 && (
          <ul className="flex flex-col divide-y rounded-lg border">
            {status.channels.map((c) => (
              <li key={c.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                <a href={c.url ?? "#"} className="font-medium hover:underline">
                  {c.name}
                </a>
                {c.is_private && <Badge variant="secondary">private</Badge>}
                {c.backfill_status !== "done" && <Badge variant="outline">{c.backfill_status}</Badge>}
                <span className="ml-auto text-muted-foreground">
                  {c.can_read ? `${c.documents} messages` : "no access"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
