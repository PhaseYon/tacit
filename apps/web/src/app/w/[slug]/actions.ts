"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, apiFetch } from "@/lib/api";

export type ConnectState = { error?: string };

/** Ask the API for a GitHub App install URL (with a signed state) and go there. */
export async function connectGitHub(
  workspaceId: string,
  slug: string,
  _prev: ConnectState,
): Promise<ConnectState> {
  let url: string;
  try {
    ({ url } = await apiFetch<{ url: string }>(`/workspaces/${workspaceId}/github/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ return_to: `/w/${slug}` }),
    }));
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) return { error: "Link your GitHub account first." };
    if (e instanceof ApiError && e.status === 403) return { error: "Only workspace admins can connect GitHub." };
    if (e instanceof ApiError && e.status === 503) return { error: "The GitHub App isn't configured on this server." };
    return { error: "Could not start the GitHub connection." };
  }
  redirect(url);
}

/** Ask the API for a Slack OAuth install URL (with a signed state) and go there. */
export async function connectSlack(
  workspaceId: string,
  slug: string,
  _prev: ConnectState,
): Promise<ConnectState> {
  let url: string;
  try {
    ({ url } = await apiFetch<{ url: string }>(`/workspaces/${workspaceId}/slack/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ return_to: `/w/${slug}` }),
    }));
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) return { error: "Link your Slack account first." };
    if (e instanceof ApiError && e.status === 403) return { error: "Only workspace admins can connect Slack." };
    if (e instanceof ApiError && e.status === 503) return { error: "The Slack app isn't configured on this server." };
    return { error: "Could not start the Slack connection." };
  }
  redirect(url);
}

/** Any member may connect their own Notion: synced pages are visible only to them. */
export async function connectNotion(
  workspaceId: string,
  slug: string,
  _prev: ConnectState,
): Promise<ConnectState> {
  let url: string;
  try {
    ({ url } = await apiFetch<{ url: string }>(`/workspaces/${workspaceId}/notion/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ return_to: `/w/${slug}` }),
    }));
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) return { error: "Notion isn't configured on this server." };
    return { error: "Could not start the Notion connection." };
  }
  redirect(url);
}

/** Widening needs an admin who owns the connection; the API enforces it. */
export async function setNotionTreeVisibility(
  workspaceId: string,
  slug: string,
  treeId: string,
  visible: boolean,
  _prev: ConnectState,
): Promise<ConnectState> {
  try {
    await apiFetch(`/workspaces/${workspaceId}/notion/trees/${treeId}/visibility`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_visible: visible }),
    });
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) return { error: "Not allowed." };
    return { error: "Could not change visibility." };
  }
  revalidatePath(`/w/${slug}`);
  return {};
}

export async function disconnectNotion(
  workspaceId: string,
  slug: string,
  connectionId: string,
  _prev: ConnectState,
): Promise<ConnectState> {
  try {
    await apiFetch(`/workspaces/${workspaceId}/notion/connections/${connectionId}`, { method: "DELETE" });
  } catch {
    return { error: "Could not disconnect." };
  }
  revalidatePath(`/w/${slug}`);
  return {};
}

export type GraphSettings = {
  enabled: boolean;
  include_private: boolean;
  monthly_budget_usd: number;
};

/** Admin-only on the API side; RLS still scopes it to this workspace. */
export async function saveGraphSettings(
  workspaceId: string,
  _prev: ConnectState,
  form: FormData,
): Promise<ConnectState> {
  const body: GraphSettings = {
    enabled: form.get("enabled") === "on",
    include_private: form.get("include_private") === "on",
    monthly_budget_usd: Number(form.get("monthly_budget_usd") ?? 5),
  };
  try {
    await apiFetch(`/workspaces/${workspaceId}/graph/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) return { error: "Only workspace admins can change this." };
    return { error: "Could not save settings." };
  }
  return {};
}
