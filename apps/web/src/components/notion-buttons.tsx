"use client";

import { useActionState } from "react";

import { Button } from "@/components/ui/button";
import {
  type ConnectState,
  connectNotion,
  disconnectNotion,
  setNotionTreeVisibility,
} from "@/app/w/[slug]/actions";

export function ConnectNotionButton({ workspaceId, slug }: { workspaceId: string; slug: string }) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    connectNotion.bind(null, workspaceId, slug),
    {},
  );
  return (
    <form action={action} className="flex flex-col gap-2">
      <Button type="submit" disabled={pending}>
        {pending ? "Opening Notion…" : "Connect your Notion"}
      </Button>
      {state.error && <p className="text-sm text-destructive">{state.error}</p>}
    </form>
  );
}

export function TreeVisibilityToggle({
  workspaceId,
  slug,
  treeId,
  visible,
}: {
  workspaceId: string;
  slug: string;
  treeId: string;
  visible: boolean;
}) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    setNotionTreeVisibility.bind(null, workspaceId, slug, treeId, !visible),
    {},
  );
  return (
    <form
      action={action}
      onSubmit={(e) => {
        const ok =
          visible ||
          window.confirm(
            "Everyone in this Tacit workspace will be able to search these pages, including sub-pages " +
              "added later and sub-pages with narrower access in Notion. Continue?",
          );
        if (!ok) e.preventDefault();
      }}
      className="flex items-center gap-2"
    >
      <Button type="submit" size="sm" variant="outline" disabled={pending}>
        {visible ? "Make private" : "Share with workspace"}
      </Button>
      {state.error && <span className="text-destructive">{state.error}</span>}
    </form>
  );
}

export function DisconnectNotionButton({
  workspaceId,
  slug,
  connectionId,
}: {
  workspaceId: string;
  slug: string;
  connectionId: string;
}) {
  const [state, action, pending] = useActionState<ConnectState, FormData>(
    disconnectNotion.bind(null, workspaceId, slug, connectionId),
    {},
  );
  return (
    <form action={action} className="flex items-center gap-2">
      <Button type="submit" size="sm" variant="ghost" disabled={pending}>
        Disconnect
      </Button>
      {state.error && <span className="text-destructive">{state.error}</span>}
    </form>
  );
}
