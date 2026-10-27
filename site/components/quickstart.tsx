import { NEO4J_COMPOSE } from "@/lib/site";
import { CodeLine } from "./copy-button";

type Step = { title: string; body: React.ReactNode; code?: string };

const link = "text-accent underline-offset-2 hover:underline";

const STEPS: Step[] = [
  {
    title: "Create your team's Neo4j",
    body: (
      <>
        A free{" "}
        <a className={link} href="https://neo4j.com/product/auradb/">
          Neo4j Aura
        </a>{" "}
        instance is enough to start. Or run{" "}
        <a className={link} href={NEO4J_COMPOSE}>
          our Docker Compose file
        </a>{" "}
        on a small VPS. Keep the URL and password.
      </>
    ),
  },
  {
    title: "Connect and set up the schema",
    body: "Asks for the URL, password and your email (used in the access log).",
    code: "uvx tacit init",
  },
  {
    title: "Sign in to GitHub",
    body: "Uses a fine-grained token, or reuses `gh auth token`.",
    code: "tacit auth github",
  },
  {
    title: "Choose what to sync",
    body: "Scope is shared by the whole team and is the permission boundary.",
    code: "tacit scope add github:owner/repo",
  },
  {
    title: "Sync",
    body: "Unchanged items are skipped by content hash, so re-runs are cheap.",
    code: "tacit sync",
  },
  {
    title: "Wire it into Claude",
    body: "Also --client cursor or --client claude-code. Then ask: “why did we do X?”",
    code: "tacit mcp install --client claude",
  },
];

function Body({ body }: { body: React.ReactNode }) {
  if (typeof body !== "string") return <>{body}</>;
  return (
    <>
      {body.split(/(`[^`]+`)/g).map((part, i) =>
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

export function Quickstart() {
  return (
    <div>
      <ol className="flex flex-col gap-5">
        {STEPS.map((s, i) => (
          <li key={s.title} className="grid grid-cols-[2rem_1fr] gap-x-3">
            <span
              aria-hidden
              className="grid size-7 place-items-center rounded-full border border-border-strong font-mono text-xs text-muted"
            >
              {i + 1}
            </span>
            <div className="min-w-0">
              <h3 className="font-medium">{s.title}</h3>
              <p className="mt-0.5 text-sm text-muted">
                <Body body={s.body} />
              </p>
              {s.code && (
                <div className="mt-2">
                  <CodeLine code={s.code} />
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
      <p className="mt-6 text-sm text-muted">
        Teammates: get the URL and password from whoever set it up, then run{" "}
        <code className="rounded bg-code-bg px-1 py-0.5 font-mono text-[0.85em] text-fg">
          uvx tacit init
        </code>{" "}
        and{" "}
        <code className="rounded bg-code-bg px-1 py-0.5 font-mono text-[0.85em] text-fg">
          tacit mcp install
        </code>
        .
      </p>
    </div>
  );
}
