"use client";

import { useActionState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { authClient } from "@/lib/auth-client";
import { type ConnectState, connectSlack } from "@/app/w/[slug]/actions";

export function LinkSlackButton({ slug }: { slug: string }) {
  return (
    <Button
      variant="outline"
      onClick={async () => {
        const { error } = await authClient.linkSocial({
          provider: "slack",
          callbackURL: `/w/${slug}`,
        });
        if (error) toast.error(error.message ?? "Could not link Slack");
      }}
    >
      Link your Slack account
    </Button>
  );
}

export function ConnectSlackButton({ workspaceId, slug }: { workspaceId: string; slug: string }) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    connectSlack.bind(null, workspaceId, slug),
    {},
  );
  return (
    <form action={action} className="flex flex-col gap-2">
      <Button type="submit" disabled={pending}>
        {pending ? "Opening Slack…" : "Connect a Slack workspace"}
      </Button>
      {state.error && <p className="text-sm text-destructive">{state.error}</p>}
    </form>
  );
}
