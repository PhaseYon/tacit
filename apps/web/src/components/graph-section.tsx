import { GraphSettingsForm } from "@/components/graph-settings-form";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch } from "@/lib/api";

type Settings = {
  enabled: boolean;
  include_private: boolean;
  monthly_budget_usd: number;
  server_enabled: boolean;
  paused_reason: string | null;
  spent_this_month_usd: number;
  episodes: Record<string, number>;
};

export async function GraphSection({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const s = await apiFetch<Settings>(`/workspaces/${workspaceId}/graph/settings`);
  const done = s.episodes.done ?? 0;
  const pending = (s.episodes.pending ?? 0) + (s.episodes.processing ?? 0);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Knowledge graph</CardTitle>
        <CardDescription>
          Decisions, people and how things changed over time, extracted from merged PRs, discussed issues and docs.
          Uses an LLM, so it costs money and is off by default.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        {!s.server_enabled && (
          <p className="text-muted-foreground">
            Disabled on this server (set GRAPH_ENABLED=true and an LLM API key to turn it on).
          </p>
        )}
        {s.paused_reason === "budget" && (
          <p className="text-destructive">Paused: this month&apos;s budget is used up.</p>
        )}
        <p className="text-muted-foreground">
          {done} episodes extracted{pending ? `, ${pending} queued` : ""} · ${s.spent_this_month_usd.toFixed(2)} of $
          {s.monthly_budget_usd.toFixed(2)} spent this month
        </p>
        {canManage && <GraphSettingsForm workspaceId={workspaceId} settings={s} />}
      </CardContent>
    </Card>
  );
}
