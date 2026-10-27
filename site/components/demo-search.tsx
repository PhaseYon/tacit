"use client";

import { useState } from "react";

import { DEMO_QUESTIONS, type DemoQuestionId } from "@/lib/demo-questions";
import type { DemoError, DemoResponse, DemoResult } from "@/lib/demo-types";

const KIND_LABEL: Record<string, string> = {
  pull_request: "PR",
  issue: "Issue",
  markdown_file: "Doc",
  slack_thread: "Slack",
  slack_day: "Slack",
  notion_page: "Notion",
};

type State =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "done"; data: DemoResponse }
  | { status: "error"; message: string };

export function DemoSearch() {
  const [active, setActive] = useState<DemoQuestionId | null>(null);
  const [state, setState] = useState<State>({ status: "idle" });
  const question = DEMO_QUESTIONS.find((q) => q.id === active)?.question;

  async function ask(id: DemoQuestionId) {
    setActive(id);
    setState({ status: "loading" });
    try {
      const res = await fetch("/api/demo-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const body = (await res.json()) as DemoResponse | DemoError;
      if (!res.ok || "error" in body) {
        setState({ status: "error", message: "error" in body ? body.error : "Search failed." });
        return;
      }
      setState({ status: "done", data: body });
    } catch {
      setState({ status: "error", message: "Couldn't reach the example. Check your connection." });
    }
  }

  return (
    <div className="rounded-2xl border border-border bg-surface p-4 shadow-sm sm:p-6">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">Pick a question</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {DEMO_QUESTIONS.map((q) => (
          <button
            key={q.id}
            type="button"
            onClick={() => ask(q.id)}
            disabled={state.status === "loading"}
            aria-pressed={active === q.id}
            className={`rounded-full border px-4 py-2 text-sm transition-colors disabled:opacity-60 ${
              active === q.id
                ? "border-accent bg-accent-soft text-fg"
                : "border-border text-muted hover:border-accent hover:text-fg"
            }`}
          >
            {q.question}
          </button>
        ))}
      </div>

      <div aria-live="polite" aria-busy={state.status === "loading"} className="mt-6">
        {state.status === "idle" && (
          <p className="text-sm text-muted">
            Tacit will pull the evidence from Larkspur Labs&apos; history.
          </p>
        )}
        {state.status === "loading" && <Skeleton />}
        {state.status === "error" && (
          <p className="rounded-lg bg-warn-soft px-4 py-3 text-sm text-warn-fg">{state.message}</p>
        )}
        {state.status === "done" && question && (
          // key restarts the entrance animation for every new question
          <Results key={active} data={state.data} />
        )}
      </div>

      <p className="mt-5 text-xs text-muted">
        Fictional company, so the links don&apos;t resolve. No LLM involved: this is what Tacit
        hands to Claude, which then writes the answer.
      </p>
    </div>
  );
}

function Results({ data }: { data: DemoResponse }) {
  if (data.results.length === 0) {
    return <p className="text-sm text-muted">Nothing found for this one.</p>;
  }
  return (
    <ol className="flex flex-col gap-3">
      {data.results.slice(0, 3).map((r, i) => (
        <ResultCard key={`${r.url}-${i}`} r={r} index={i} />
      ))}
    </ol>
  );
}

function ResultCard({ r, index }: { r: DemoResult; index: number }) {
  return (
    <li
      className="animate-rise rounded-xl border border-border bg-bg p-4"
      style={{ animationDelay: `${index * 120}ms` }}
    >
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="rounded bg-surface-2 px-1.5 py-0.5 font-medium text-fg">
          {KIND_LABEL[r.kind] ?? r.kind}
        </span>
        {r.repo && <span className="font-mono">{r.repo}</span>}
        {r.date && <span>· {r.date}</span>}
      </div>
      <p className="mt-2 font-medium text-fg">{r.title}</p>
      <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-muted">{r.snippet}</p>
    </li>
  );
}

function Skeleton() {
  return (
    <div className="flex flex-col gap-3" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-20 animate-pulse rounded-xl bg-surface-2"
          style={{ animationDelay: `${i * 120}ms` }}
        />
      ))}
    </div>
  );
}
