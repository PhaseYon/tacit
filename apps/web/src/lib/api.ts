import "server-only";

import { headers } from "next/headers";

import { auth } from "./auth";

const API_URL = process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/**
 * Call the FastAPI backend as the signed-in user. Better Auth mints a short-lived
 * JWT, and FastAPI verifies it against /api/auth/jwks.
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { token } = await auth.api.getToken({ headers: await headers() });
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json() as Promise<T>;
}

export type ApiMember = {
  user_id: string;
  name: string;
  email: string;
  role: string;
  joined_at: string;
};
