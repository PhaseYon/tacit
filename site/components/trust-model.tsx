import { CodeLine } from "./copy-button";

const POINTS = [
  {
    title: "One shared password per team graph",
    body: "Whoever has your team's Neo4j URL and password can read the whole graph. Tacit is for small, high-trust teams; handing the password out carefully is your team's job. There are no per-user permissions inside a graph.",
  },
  {
    title: "Sync scope is the boundary",
    body: "Nothing outside `tacit scope` is ever fetched. GitHub repos and Notion pages must be listed explicitly. Slack is public channels only by default; private channels need --private and a warning. DMs are never synced. You can only sync what your own token can read.",
  },
  {
    title: "Every query is logged, first",
    body: "Every read and write goes through one function that writes an access entry (who, tool, params, when) before the query runs. If the log write fails, the query doesn't run. The LLM never writes Cypher.",
  },
  {
    title: "Your keys stay on your laptop",
    body: "Source tokens live in your OS keychain (or a 0600 file where there's none). Graph extraction is optional and uses your own LLM key. Tacit sends us no telemetry.",
  },
];

const GAPS = [
  "Anyone with the password can query Neo4j directly (Neo4j Browser, cypher-shell), skip the log, or delete log entries.",
  "The identity in the log is self-asserted (the email you gave `tacit init`).",
  "The access log is not tamper-proof. Closing that needs per-person database logins or a gateway, which is out of scope for now.",
  "If someone shouldn't see a channel or repo, keep it out of scope. There is no finer-grained control.",
];

function Inline({ text }: { text: string }) {
  // Render `code` spans inside plain strings.
  return (
    <>
      {text.split(/(`[^`]+`)/g).map((part, i) =>
        part.startsWith("`") ? (
          <code key={i} className="rounded bg-code-bg px-1 py-0.5 font-mono text-[0.85em] text-fg">
            {part.slice(1, -1)}
          </code>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function TrustModel() {
  return (
    <div className="flex flex-col gap-8">
      <div className="grid gap-4 sm:grid-cols-2">
        {POINTS.map((p) => (
          <div key={p.title} className="rounded-xl border border-border bg-surface p-5">
            <h3 className="font-semibold">{p.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              <Inline text={p.body} />
            </p>
          </div>
        ))}
      </div>

      <div>
        <p className="mb-2 text-sm text-muted">Read the log from any laptop on the team:</p>
        <CodeLine code="tacit log --who alice@ --since 7d --grep redis" />
      </div>

      <div className="rounded-xl border border-border bg-surface-2 p-5">
        <h3 className="font-semibold">Known gaps, stated plainly</h3>
        <ul className="mt-3 list-disc space-y-1.5 pl-5 text-sm text-muted">
          {GAPS.map((g) => (
            <li key={g}>
              <Inline text={g} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
