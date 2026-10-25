"use client";

import { useActionState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { authClient } from "@/lib/auth-client";
import { type ConnectState, connectGitHub } from "@/app/w/[slug]/actions";

export function LinkGitHubButton({ slug }: { slug: string }) {
  return (
    <Button
      variant="outline"
      onClick={async () => {
        const { error } = await authClient.linkSocial({
          provider: "github",
          callbackURL: `/w/${slug}`,
        });
        if (error) toast.error(error.message ?? "Could not link GitHub");
      }}
    >
      Link your GitHub account
    </Button>
  );
}

export function ConnectGitHubButton({ workspaceId, slug }: { workspaceId: string; slug: string }) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    connectGitHub.bind(null, workspaceId, slug),
    {},
  );
  return (
    <form action={action} className="flex flex-col gap-2">
      <Button type="submit" disabled={pending}>
        {pending ? "Opening GitHub…" : "Connect GitHub repositories"}
      </Button>
      {state.error && <p className="text-sm text-destructive">{state.error}</p>}
    </form>
  );
}
