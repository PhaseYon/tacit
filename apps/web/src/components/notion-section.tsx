import {
  ConnectNotionButton,
  DisconnectNotionButton,
  TreeVisibilityToggle,
} from "@/components/notion-buttons";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch } from "@/lib/api";

type Connection = {
  id: string;
  notion_workspace_name: string | null;
  owner_name: string | null;
  is_mine: boolean;
};
type Tree = {
  id: string;
  connection_id: string;
  title: string;
  url: string | null;
  workspace_visible: boolean;
  is_mine: boolean;
  can_read: boolean;
  documents: number;
};
type Status = { configured: boolean; is_admin: boolean; connections: Connection[]; trees: Tree[] };

const ERRORS: Record<string, string> = {
  expired: "The connection link expired. Please try again.",
  cancelled: "The Notion connection was cancelled.",
  not_allowed: "Only connections owned by a person are supported (not workspace-owned tokens).",
  taken: "That Notion authorization is already connected to another Tacit workspace.",
  failed: "Something went wrong connecting Notion.",
};

export async function NotionSection({
  workspaceId,
  slug,
  flash,
}: {
  workspaceId: string;
  slug: string;
  flash: { connected?: boolean; error?: string };
}) {
  const status = await apiFetch<Status>(`/workspaces/${workspaceId}/notion`);
  const mine = status.connections.filter((c) => c.is_mine);
  const others = status.connections.filter((c) => !c.is_mine);
  // Trees you can see: your own, and ones an admin shared with the workspace.
  const trees = status.trees.filter((t) => t.is_mine || t.can_read);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Notion</CardTitle>
        <CardDescription>
          Pages you share with Tacit in Notion&apos;s page picker. Notion doesn&apos;t tell us who else can read a
          page, so what you connect is visible only to you until a workspace admin who connected it shares a page
          tree with the workspace.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {flash.connected && (
          <p className="text-sm text-emerald-600 dark:text-emerald-400">
            Notion connected. Your pages are syncing and are visible only to you.
          </p>
        )}
        {flash.error && <p className="text-sm text-destructive">{ERRORS[flash.error] ?? ERRORS.failed}</p>}

        {!status.configured ? (
          <p className="text-sm text-muted-foreground">
            The Notion integration isn&apos;t configured on this server yet (see docs/design/notion-connector.md).
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <ConnectNotionButton workspaceId={workspaceId} slug={slug} />
          </div>
        )}

        {mine.length > 0 && (
          <ul className="flex flex-col gap-1 text-sm">
            {mine.map((c) => (
              <li key={c.id} className="flex items-center gap-2">
                <span>Your connection: {c.notion_workspace_name ?? "Notion"}</span>
                <DisconnectNotionButton workspaceId={workspaceId} slug={slug} connectionId={c.id} />
              </li>
            ))}
          </ul>
        )}
        {others.length > 0 && (
          <p className="text-sm text-muted-foreground">
            Also connected by: {others.map((c) => c.owner_name ?? "a teammate").join(", ")}
          </p>
        )}

        {trees.length > 0 && (
          <ul className="flex flex-col divide-y rounded-lg border">
            {trees.map((t) => (
              <li key={t.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                <a href={t.url ?? "#"} className="font-medium hover:underline">
                  {t.title}
                </a>
                <Badge variant={t.workspace_visible ? "default" : "secondary"}>
                  {t.workspace_visible ? "visible to workspace" : "only you"}
                </Badge>
                <span className="ml-auto text-muted-foreground">{t.documents} pages</span>
                {/* Widening: admin AND owner. Narrowing: owner or admin. The API enforces both. */}
                {((t.is_mine && status.is_admin) || (t.workspace_visible && (t.is_mine || status.is_admin))) && (
                  <TreeVisibilityToggle
                    workspaceId={workspaceId}
                    slug={slug}
                    treeId={t.id}
                    visible={t.workspace_visible}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
