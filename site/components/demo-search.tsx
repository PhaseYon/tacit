"use client";

import { useId, useState, type FormEvent } from "react";

import {
  MAX_QUERY_CHARS,
  type DemoError,
  type DemoResponse,
  type DemoResult,
} from "@/lib/demo-types";

const KIND_LABEL: Record<string, string> = {
  pull_request: "PR",
  issue: "Issue",
  issue_comment: "Comment",
  pr_review: "Review",
  pr_review_comment: "Review comment",
  markdown_file: "Doc",
  slack_message: "Slack",
  slack_reply: "Slack reply",
  notion_page: "Notion",
};

type State =
  | { status: "idle" }
  | { status: "loading"; query: string }
  | { status: "done"; data: DemoResponse }
  | { status: "error"; message: string };

export function DemoSearch({ suggestions }: { suggestions: string[] }) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState<State>({ status: "idle" });
  const inputId = useId();
  const hintId = useId();

  async function run(q: string) {
    const trimmed = q.trim();
    if (!trimmed) return;
    setState({ status: "loading", query: trimmed });
    try {
      const res = await fetch("/api/demo-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: trimmed }),
      });
      const body = (await res.json()) as DemoResponse | DemoError;
      if (!res.ok || "error" in body) {
        setState({ status: "error", message: "error" in body ? body.error : "Search failed." });
        return;
      }
      setState({ status: "done", data: body });
    } catch {
      setState({ status: "error", message: "Couldn't reach the search. Check your connection." });
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    run(query);
  }

  return (
    <div className="rounded-2xl border border-border bg-surface p-4 shadow-sm sm:p-6">
      <form onSubmit={onSubmit} role="search" className="flex flex-col gap-2 sm:flex-row">
        <label htmlFor={inputId} className="sr-only">
          Ask the Larkspur Labs demo graph a question
        </label>
        <input
          id={inputId}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={MAX_QUERY_CHARS}
          placeholder="Why did we replace Elasticsearch?"
          aria-describedby={hintId}
          autoComplete="off"
          className="min-w-0 flex-1 rounded-lg border border-border-strong bg-bg px-4 py-2.5 text-base placeholder:text-muted/70 focus:border-accent focus:outline-none"
        />
        <button
          type="submit"
          disabled={state.status === "loading"}
          className="rounded-lg bg-accent px-5 py-2.5 font-medium text-accent-fg transition-colors hover:bg-accent-hover disabled:opacity-60"
        >
          {state.status === "loading" ? "Searching…" : "Search"}
        </button>
      </form>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Try:</span>
        {suggestions.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => {
              setQuery(s);
              run(s);
            }}
            className="rounded-full border border-border px-3 py-1 text-xs text-muted transition-colors hover:border-accent hover:text-fg"
          >
            {s}
          </button>
        ))}
      </div>

      <p id={hintId} className="mt-3 text-xs text-muted">
        Fictional data only, so the citation links don&apos;t resolve. This box runs keyword
        (full-text) search over the demo graph&apos;s chunk index, with no LLM. The CLI also runs
        vector search and fuses the two.
      </p>

      <div aria-live="polite" aria-busy={state.status === "loading"} className="mt-4">
        {state.status === "error" && (
          <p className="rounded-lg bg-warn-soft px-4 py-3 text-sm text-warn-fg">{state.message}</p>
        )}
        {state.status === "loading" && <ResultsSkeleton />}
        {state.status === "done" && <Results data={state.data} />}
      </div>
    </div>
  );
}

function Results({ data }: { data: DemoResponse }) {
  return (
    <div>
      {data.mode === "example" ? (
        <p className="mb-3 rounded-lg bg-warn-soft px-4 py-3 text-sm text-warn-fg">
          <strong className="font-semibold">Example results.</strong>{" "}
          {data.reason === "not_configured"
            ? "The live demo database isn't connected to this deployment"
            : "The live demo database didn't answer"}
          , so these are recorded results for{" "}
          <q className="font-medium">{data.exampleQuery}</q>.
        </p>
      ) : (
        <p className="mb-3 text-xs text-muted">
          <span className="mr-1.5 inline-block size-2 rounded-full bg-accent align-middle" />
          Live · {data.results.length} result{data.results.length === 1 ? "" : "s"} ·{" "}
          {data.tookMs} ms
        </p>
      )}
      {data.results.length === 0 ? (
        <p className="text-sm text-muted">
          No matches. Try words that appear in the docs, like an ADR number or a product name.
        </p>
      ) : (
        <ol className="flex flex-col gap-3">
          {data.results.map((r, i) => (
            <ResultItem key={`${r.url}-${i}`} r={r} />
          ))}
        </ol>
      )}
    </div>
  );
}

function ResultItem({ r }: { r: DemoResult }) {
  return (
    <li className="rounded-lg border border-border bg-bg p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <span className="rounded bg-surface-2 px-1.5 py-0.5 font-medium text-fg">
          {KIND_LABEL[r.kind] ?? r.kind}
        </span>
        {r.repo && <span className="font-mono">{r.repo}</span>}
        {r.date && <span>· {r.date}</span>}
      </div>
      <p className="mt-1.5 font-medium text-fg">{r.title}</p>
      <p className="mt-1 text-sm leading-relaxed text-muted">{r.snippet}</p>
      {/* The demo company is fictional, so its citation links don't resolve; show them as text. */}
      <p className="mt-2 truncate font-mono text-xs text-muted/80" title={r.url}>
        <span className="sr-only">Citation: </span>
        {r.url}
      </p>
    </li>
  );
}

function ResultsSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-24 animate-pulse rounded-lg bg-surface-2" />
      ))}
    </div>
  );
}
