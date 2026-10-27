import { NextResponse, type NextRequest } from "next/server";

import { questionFor } from "@/lib/demo-questions";
import { demoSearch } from "@/lib/demo-search";
import type { DemoError } from "@/lib/demo-types";
import { clientIp, take } from "@/lib/rate-limit";

// Runs on the Node.js runtime (the default; the `runtime` segment option is not allowed
// with cacheComponents). The Neo4j driver needs TCP sockets, so never move this to edge.

const NO_STORE = { "Cache-Control": "no-store" };

async function handle(req: NextRequest, id: unknown) {
  const limit = take(clientIp(req.headers));
  if (!limit.ok) {
    return NextResponse.json<DemoError>(
      { error: "Too many searches. Try again in a moment.", retryAfter: limit.retryAfter },
      { status: 429, headers: { ...NO_STORE, "Retry-After": String(limit.retryAfter) } },
    );
  }
  const question = questionFor(id);
  if (!question) {
    return NextResponse.json<DemoError>(
      { error: "Pick one of the example questions." },
      { status: 400, headers: NO_STORE },
    );
  }
  const body = await demoSearch(question.terms);
  return NextResponse.json({ ...body, query: question.question }, { headers: NO_STORE });
}

export async function POST(req: NextRequest) {
  const len = Number(req.headers.get("content-length") ?? 0);
  if (len > 1_024) {
    return NextResponse.json<DemoError>({ error: "Request too large." }, { status: 413 });
  }
  let id: unknown;
  try {
    id = ((await req.json()) as { id?: unknown })?.id;
  } catch {
    id = undefined;
  }
  return handle(req, id);
}

// GET ?id=… is what the daily keep-alive workflow calls, so Aura Free never sits idle.
export async function GET(req: NextRequest) {
  return handle(req, req.nextUrl.searchParams.get("id"));
}
