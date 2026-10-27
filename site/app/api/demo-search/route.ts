import { NextResponse, type NextRequest } from "next/server";

import { demoSearch } from "@/lib/demo-search";
import { MAX_QUERY_CHARS, type DemoError } from "@/lib/demo-types";
import { clientIp, take } from "@/lib/rate-limit";

// Node runtime: the Neo4j driver needs TCP sockets.
export const runtime = "nodejs";

const NO_STORE = { "Cache-Control": "no-store" };

async function handle(req: NextRequest, raw: unknown) {
  const limit = take(clientIp(req.headers));
  if (!limit.ok) {
    return NextResponse.json<DemoError>(
      { error: "Too many searches. Try again in a moment.", retryAfter: limit.retryAfter },
      { status: 429, headers: { ...NO_STORE, "Retry-After": String(limit.retryAfter) } },
    );
  }
  if (typeof raw !== "string" || raw.trim() === "") {
    return NextResponse.json<DemoError>(
      { error: "Send a question as `q`." },
      { status: 400, headers: NO_STORE },
    );
  }
  const q = raw.trim();
  if (q.length > MAX_QUERY_CHARS) {
    return NextResponse.json<DemoError>(
      { error: `Keep it under ${MAX_QUERY_CHARS} characters.` },
      { status: 413, headers: NO_STORE },
    );
  }
  const body = await demoSearch(q);
  return NextResponse.json(body, { headers: NO_STORE });
}

export async function POST(req: NextRequest) {
  // Reject big bodies before parsing them.
  const len = Number(req.headers.get("content-length") ?? 0);
  if (len > 4_096) {
    return NextResponse.json<DemoError>({ error: "Request too large." }, { status: 413 });
  }
  let q: unknown;
  try {
    q = ((await req.json()) as { q?: unknown })?.q;
  } catch {
    q = undefined;
  }
  return handle(req, q);
}

// GET ?q=… is what the daily keep-alive workflow calls, so Aura Free never sits idle.
export async function GET(req: NextRequest) {
  return handle(req, req.nextUrl.searchParams.get("q"));
}
