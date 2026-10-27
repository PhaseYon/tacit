import "server-only";

import neo4j, { type Driver } from "neo4j-driver";

import examples from "@/data/demo-examples.json";
import type { DemoResponse, DemoResult } from "./demo-types";

// Keyword (full-text) search over the demo graph's chunk index, the same index the CLI's
// `tacit search` uses (:TacitChunk, full-text index `tacit_chunk_text` on text + title).
//
// Vector search is deliberately left out: matching the CLI means embedding the query with
// bge-small-en-v1.5 in Node, and the ONNX runtime plus model are too heavy for a
// serverless function. The v0 benchmark had keyword-only ahead of hybrid anyway.
//
// Visitors never send Cypher. The query below is fixed; the visitor's text only reaches
// it as a sanitized Lucene term list passed as a parameter.

const SEARCH_CYPHER = `
CALL db.index.fulltext.queryNodes('tacit_chunk_text', $q, {limit: $fetch})
YIELD node, score
MATCH (d:TacitDoc)-[:HAS_CHUNK]->(node)
WITH d, node, score ORDER BY score DESC
WITH d, collect({text: node.text, score: score})[0] AS best
RETURN d.title AS title, d.url AS url, d.source AS source, d.kind AS kind,
       d.scope_key AS scope, toString(d.reference_time) AS date,
       best.text AS text, best.score AS score
ORDER BY score DESC
LIMIT $limit
`;

const LIMIT = 8;
const MAX_TERMS = 16;
const QUERY_TIMEOUT_MS = 5_000;

let driver: Driver | null = null;

function getDriver(): Driver | null {
  const uri = process.env.DEMO_NEO4J_URI;
  const user = process.env.DEMO_NEO4J_USER;
  const password = process.env.DEMO_NEO4J_PASSWORD;
  if (!uri || !user || !password) return null;
  driver ??= neo4j.driver(uri, neo4j.auth.basic(user, password), {
    connectionTimeout: 5_000,
    maxConnectionPoolSize: 5,
    disableLosslessIntegers: true,
  });
  return driver;
}

/** Words only: no Lucene operators, quotes, wildcards or field names survive. */
export function toTerms(query: string): string[] {
  const words = query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  const terms = [...new Set(words.filter((w) => w.length > 1 || /\d/.test(w)))];
  // Lucene keywords would act as operators even without symbols.
  return terms.filter((w) => !["and", "or", "not", "to"].includes(w)).slice(0, MAX_TERMS);
}

export function makeSnippet(text: string, terms: string[], max = 260): string {
  const clean = text
    .replace(/^\s*(#{1,6}|>|[-*])\s+/gm, " ")
    .replace(/\*\*|`/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (clean.length <= max) return clean;
  const lower = clean.toLowerCase();
  const hit = terms
    .filter((t) => t.length > 3)
    .map((t) => lower.indexOf(t))
    .filter((i) => i >= 0)
    .sort((a, b) => a - b)[0];
  let start = hit === undefined ? 0 : Math.max(0, hit - 60);
  if (start > 0) {
    const space = clean.indexOf(" ", start);
    start = space >= 0 && space < hit! ? space + 1 : start;
  }
  let out = clean.slice(start, start + max);
  if (start + max < clean.length) out = out.replace(/\s+\S*$/, "") + "…";
  return (start > 0 ? "…" : "") + out;
}

function repoFromScope(scope: unknown): string | null {
  if (typeof scope !== "string") return null;
  const m = scope.match(/^github:(.+)$/);
  return m ? m[1] : scope;
}

export function exampleFor(
  query: string,
  reason: "not_configured" | "unavailable",
): DemoResponse {
  const q = new Set(toTerms(query));
  let best = examples.examples[0];
  let bestScore = -1;
  for (const ex of examples.examples) {
    const words = [...ex.query.toLowerCase().matchAll(/[\p{L}\p{N}]+/gu)].map((m) => m[0]);
    const text = (ex.query + " " + ex.results.map((r) => r.title).join(" ")).toLowerCase();
    const score = [...q].filter((t) => t.length > 3 && (words.includes(t) || text.includes(t))).length;
    if (score > bestScore) {
      best = ex;
      bestScore = score;
    }
  }
  return {
    mode: "example",
    query,
    reason,
    exampleQuery: best.query,
    results: best.results as DemoResult[],
  };
}

export async function demoSearch(query: string): Promise<DemoResponse> {
  const terms = toTerms(query);
  const d = getDriver();
  if (!d) return exampleFor(query, "not_configured");
  if (terms.length === 0) {
    return { mode: "live", query, search: "keyword", results: [], tookMs: 0 };
  }

  const started = Date.now();
  const session = d.session({
    defaultAccessMode: neo4j.session.READ,
    database: process.env.DEMO_NEO4J_DATABASE || undefined,
  });
  try {
    const res = await session.executeRead(
      (tx) =>
        tx.run(SEARCH_CYPHER, {
          q: terms.join(" "),
          fetch: neo4j.int(LIMIT * 6),
          limit: neo4j.int(LIMIT),
        }),
      { timeout: QUERY_TIMEOUT_MS },
    );
    const results: DemoResult[] = res.records.map((r) => ({
      title: String(r.get("title") ?? "Untitled"),
      url: String(r.get("url") ?? ""),
      source: String(r.get("source") ?? ""),
      kind: String(r.get("kind") ?? ""),
      repo: repoFromScope(r.get("scope")),
      date: r.get("date") ? String(r.get("date")).slice(0, 10) : null,
      snippet: makeSnippet(String(r.get("text") ?? ""), terms),
    }));
    return { mode: "live", query, search: "keyword", results, tookMs: Date.now() - started };
  } catch (err) {
    console.error("demo-search: live query failed, serving examples:", (err as Error).message);
    return exampleFor(query, "unavailable");
  } finally {
    await session.close().catch(() => {});
  }
}
