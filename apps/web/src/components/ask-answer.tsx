import Markdown from "react-markdown";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export type AskSource = { n: number; kind: string; title: string | null; url: string; text: string; meta: string; cited: boolean };
export type AskResult = {
  mode: "generated" | "sources_only";
  answer: string;
  insufficient: boolean;
  sources: AskSource[];
  cost_usd: number;
  note: string | null;
};

/** Turn [n] markers into links to the cited source; markdown is rendered without raw HTML. */
function linkCitations(answer: string, sources: AskSource[]) {
  const byN = new Map(sources.map((s) => [s.n, s.url]));
  return answer.replace(/\[(\d+)\]/g, (m, n) => (byN.has(Number(n)) ? `[[${n}]](${byN.get(Number(n))})` : m));
}

export function AskAnswer({ result }: { result: AskResult }) {
  const cited = result.sources.filter((s) => s.cited);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Answer</CardTitle>
        <CardDescription>
          {result.mode === "generated"
            ? `Generated from ${result.sources.length} sources you can access${result.cost_usd ? ` · $${result.cost_usd.toFixed(4)}` : ""}`
            : (result.note ?? "Showing the most relevant sources.")}
        </CardDescription>
      </CardHeader>
      {result.mode === "generated" && (
        <CardContent className="flex flex-col gap-4">
          <div className="text-sm leading-relaxed [&_a]:text-primary [&_a]:underline [&_p]:mb-2 [&_ul]:list-disc [&_ul]:pl-5">
            <Markdown
              components={{
                a: ({ href, children }) => (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                ),
              }}
            >
              {linkCitations(result.answer, result.sources)}
            </Markdown>
          </div>
          {cited.length > 0 && (
            <ol className="flex flex-col gap-1 border-t pt-3 text-xs text-muted-foreground">
              {cited.map((s) => (
                <li key={s.n}>
                  [{s.n}]{" "}
                  <a href={s.url} target="_blank" rel="noreferrer" className="hover:underline">
                    {s.title ?? s.url}
                  </a>{" "}
                  · {s.meta}
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      )}
    </Card>
  );
}
