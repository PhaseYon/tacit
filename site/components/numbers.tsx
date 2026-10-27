import bench from "@/data/benchmark.json";
import { BENCHMARK_DIR, repoPath } from "@/lib/site";

type Arm = {
  id: string;
  name: string;
  status: string;
  n?: number | null;
  "recall@5"?: number | null;
  "recall@10"?: number | null;
  gold_coverage?: number | null;
  mrr?: number | null;
  p50_ms?: number | null;
  p95_ms?: number | null;
};

const COLS: { key: keyof Arm; label: string; title: string; ms?: boolean }[] = [
  { key: "recall@5", label: "Recall@5", title: "Share of questions with a gold source in the top 5" },
  { key: "recall@10", label: "Recall@10", title: "Share of questions with a gold source in the top 10" },
  { key: "gold_coverage", label: "Gold coverage", title: "Share of all gold sources found in the top 10" },
  { key: "mrr", label: "MRR", title: "Mean reciprocal rank of the first gold source" },
  { key: "p50_ms", label: "p50 ms", title: "Median query latency", ms: true },
];

function fmt(v: unknown, ms?: boolean) {
  if (typeof v !== "number") return "—";
  return ms ? Math.round(v).toString() : v.toFixed(3);
}

function ArmTable({ arms, caption }: { arms: Arm[]; caption: string }) {
  const best = Object.fromEntries(
    COLS.filter((c) => !c.ms).map((c) => [
      c.key,
      Math.max(...arms.map((a) => (typeof a[c.key] === "number" ? (a[c.key] as number) : -1))),
    ]),
  );
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[36rem] text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-surface-2 text-left text-xs text-muted">
          <tr>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Arm
            </th>
            {COLS.map((c) => (
              <th key={c.key} scope="col" title={c.title} className="px-3 py-2.5 text-right font-medium">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {arms.map((a) => (
            <tr key={a.id} className="border-t border-border bg-surface">
              <th scope="row" className="px-4 py-2.5 text-left font-medium">
                {a.name}
              </th>
              {a.status === "pending" ? (
                <td colSpan={COLS.length} className="px-3 py-2.5 text-right">
                  <span className="rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn-fg">
                    pending
                  </span>
                </td>
              ) : (
                COLS.map((c) => {
                  const v = a[c.key];
                  const isBest = !c.ms && typeof v === "number" && v === best[c.key];
                  return (
                    <td
                      key={c.key}
                      className={
                        "px-3 py-2.5 text-right font-mono tabular-nums " +
                        (isBest ? "font-semibold text-accent" : "")
                      }
                    >
                      {fmt(v, c.ms)}
                    </td>
                  );
                })
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecallBars({ arms }: { arms: Arm[] }) {
  const measured = arms.filter((a) => typeof a["recall@10"] === "number");
  return (
    <div className="flex flex-col gap-2.5" role="img" aria-label={
      "Recall at 10: " + measured.map((a) => `${a.name} ${fmt(a["recall@10"])}`).join(", ")
    }>
      {measured.map((a) => {
        const v = a["recall@10"] as number;
        return (
          <div key={a.id} className="grid grid-cols-[minmax(0,9rem)_1fr_3rem] items-center gap-3 text-xs sm:grid-cols-[minmax(0,14rem)_1fr_3rem]">
            <span className="truncate text-muted">{a.name}</span>
            <span className="h-2.5 rounded-full bg-surface-2">
              <span className="block h-full rounded-full bg-accent" style={{ width: `${v * 100}%` }} />
            </span>
            <span className="text-right font-mono tabular-nums">{fmt(v)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function Numbers() {
  const v0 = bench.v0;
  const next = bench.next;
  const date = v0?.date ? v0.date.slice(0, 10) : null;
  const r10 = (id: string) => {
    const v = (v0?.arms as Arm[] | undefined)?.find((a) => a.id === id)?.["recall@10"];
    return typeof v === "number" ? v : null;
  };
  const signed = (x: number) => (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(1);

  return (
    <div className="flex flex-col gap-8">
      <div className="rounded-xl border border-warn-fg/30 bg-warn-soft px-4 py-3 text-sm text-warn-fg">
        <strong className="font-semibold">Honest status:</strong> the only measured numbers so far
        are from the old hosted stack. They&apos;re shown as a <em>v0 (Postgres) baseline</em>. The
        benchmark on the new local-first stack is pending, and we won&apos;t show numbers until
        it has run.
      </div>

      {v0 && (
        <section aria-labelledby="v0-title" className="flex flex-col gap-4">
          <div>
            <h3 id="v0-title" className="font-semibold">
              {v0.label}{" "}
              <span className="font-normal text-muted">· new stack benchmark pending</span>
            </h3>
            <p className="mt-1 text-sm text-muted">
              {v0.stack}. {v0.questions} questions over {v0.documents} documents from the fictional
              Larkspur Labs dataset, local {v0.embedder}, k = {v0.k}, cost $0
              {date ? `, run ${date}` : ""}. Retrieval only, not answer quality.
            </p>
          </div>
          <RecallBars arms={v0.arms as Arm[]} />
          <ArmTable arms={v0.arms as Arm[]} caption={`${v0.label} retrieval results`} />
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
            {r10("hybrid") !== null && r10("vector") !== null && (
              <li>
                Hybrid vs vector-only RAG: {signed((r10("hybrid")! - r10("vector")!) * 100)} points
                recall@10 on this set.
              </li>
            )}
            {(r10("keyword") ?? 0) > (r10("hybrid") ?? 1) && (
              <li>
                Keyword-only beat hybrid, which we didn&apos;t expect. The questions and corpus
                were written by the same LLM, so they share exact terms; fusion weights are
                untuned on purpose (no held-out set yet).
              </li>
            )}
            <li>The graph-facts arm hasn&apos;t run, so we make no claim that the graph helps yet.</li>
            <li>Small dataset: latency at this size means little.</li>
          </ul>
          <p className="text-sm">
            <a className="text-accent underline-offset-2 hover:underline" href={repoPath(v0.report)}>
              Per-question results
            </a>{" "}
            ·{" "}
            <a className="text-accent underline-offset-2 hover:underline" href={BENCHMARK_DIR}>
              How to reproduce
            </a>
          </p>
        </section>
      )}

      <section aria-labelledby="next-title" className="flex flex-col gap-4">
        <div>
          <h3 id="next-title" className="font-semibold">
            {next.label}{" "}
            <span className="font-normal text-muted">
              · {next.status === "pending" ? "pending" : "measured"}
            </span>
          </h3>
          <p className="mt-1 text-sm text-muted">
            The same questions, re-run against Neo4j. Also to be measured: cost per
            ingested item with extraction on, and setup time from a clean machine.
          </p>
        </div>
        <ArmTable arms={next.arms as Arm[]} caption={`${next.label} retrieval results`} />
      </section>
    </div>
  );
}
