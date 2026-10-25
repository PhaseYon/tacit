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
