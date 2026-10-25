"use server";

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
