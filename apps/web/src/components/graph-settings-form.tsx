"use client";

import { useActionState } from "react";

import { type ConnectState, type GraphSettings, saveGraphSettings } from "@/app/w/[slug]/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function GraphSettingsForm({ workspaceId, settings }: { workspaceId: string; settings: GraphSettings }) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    saveGraphSettings.bind(null, workspaceId),
    {},
  );
  return (
    <form action={action} className="flex flex-col gap-3 text-sm">
      <label className="flex items-center gap-2">
        <input type="checkbox" name="enabled" defaultChecked={settings.enabled} />
        Build the knowledge graph for this workspace
      </label>
      <label className="flex items-start gap-2">
        <input type="checkbox" name="include_private" defaultChecked={settings.include_private} className="mt-1" />
        <span>
          Include private repositories
          <span className="block text-muted-foreground">
            Their text is sent to the configured LLM provider for extraction. Facts stay visible only to people
            with access to the repo.
          </span>
        </span>
      </label>
      <div className="flex items-center gap-2">
        <Label htmlFor="budget">Monthly budget (USD)</Label>
        <Input
          id="budget"
          name="monthly_budget_usd"
          type="number"
          min={0}
          step="0.5"
          defaultValue={settings.monthly_budget_usd}
          className="w-28"
        />
      </div>
      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={pending}>
          {pending ? "Saving…" : "Save"}
        </Button>
        {state.error && <span className="text-destructive">{state.error}</span>}
      </div>
    </form>
  );
}
