import bench from "@/data/benchmark.json";
import { BENCHMARK_DIR } from "@/lib/site";

// Every figure comes from benchmark/results and benchmark/extraction (via
// scripts/benchmark-data.mjs). The full methodology and caveats live in the repo.

type Arm = {
  id: string;
  status: string;
  n?: number | null;
  "recall@10"?: number | null;
  p50_ms?: number | null;
};

export function Numbers() {
  const h = (bench.next.arms as Arm[]).find((a) => a.id === "hybrid" && a.status === "measured");
  const extraction =
    "extraction" in bench ? (bench.extraction as { cost_usd: number } | null) : null;
  const n = h?.n ?? 0;
  const recall = h?.["recall@10"];

  const tiles: { value: string; label: string }[] = [];
  if (typeof recall === "number" && n) {
    tiles.push({
      value: `${Math.round(recall * 100)}%`,
      label: `of questions found the right source in the top 10 (${Math.round(recall * n)} of ${n})`,
    });
  }
  if (typeof h?.p50_ms === "number") {
    tiles.push({ value: `${Math.round(h.p50_ms)} ms`, label: "typical search, on a laptop" });
  }
  tiles.push({ value: "$0", label: "to search: local embeddings, no API calls" });
  if (extraction) {
    tiles.push({
      value: `$${extraction.cost_usd.toFixed(3)}`,
      label: "to turn a decision record into graph facts with Claude",
    });
  }

  if (tiles.length < 3) {
    return <p className="text-muted">Numbers coming soon.</p>;
  }

  return (
    <div>
      <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {tiles.map((t) => (
          <div key={t.label} className="flex flex-col rounded-2xl border border-border bg-surface p-6">
            <dd className="order-1 text-4xl font-semibold tracking-tight text-accent tabular-nums sm:text-5xl">
              {t.value}
            </dd>
            <dt className="order-2 mt-3 text-sm text-muted">{t.label}</dt>
          </div>
        ))}
      </dl>
      <p className="mt-5 text-sm text-muted">
        Measured on {n || 52} &ldquo;why did we…&rdquo; questions about a fictional company.{" "}
        <a className="text-accent underline-offset-2 hover:underline" href={BENCHMARK_DIR}>
          How we measured →
        </a>
      </p>
    </div>
  );
}
