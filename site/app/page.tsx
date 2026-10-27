import { Suspense } from "react";

import { CodeLine } from "@/components/copy-button";
import { DemoSearch } from "@/components/demo-search";
import { HowItWorks } from "@/components/how-it-works";
import { LiveStats, LiveStatsFallback } from "@/components/live-stats";
import { Numbers } from "@/components/numbers";
import { Quickstart } from "@/components/quickstart";
import { ThemeToggle } from "@/components/theme-toggle";
import { TrustModel } from "@/components/trust-model";
import { DEMO_README, DESIGN_DOC, INSTALL_CMD, NEO4J_COMPOSE, REPO_URL } from "@/lib/site";

const NAV = [
  { href: "#demo", label: "Example" },
  { href: "#how", label: "How it works" },
  { href: "#numbers", label: "Numbers" },
  { href: "#trust", label: "Trust" },
  { href: "#quickstart", label: "Quickstart" },
];

function GitHubIcon({ size = 18 }: { size?: number }) {
  return (
    <svg aria-hidden width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.53-1.33-1.28-1.69-1.28-1.69-1.05-.71.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.56-.29-5.25-1.28-5.25-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.42-2.7 5.39-5.26 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5Z" />
    </svg>
  );
}

function Section({
  id,
  eyebrow,
  title,
  intro,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  intro?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="border-t border-border py-16 sm:py-20">
      <p className="font-mono text-xs tracking-wider text-accent uppercase">{eyebrow}</p>
      <h2 id={`${id}-title`} className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
        {title}
      </h2>
      {intro && <p className="mt-3 max-w-2xl text-muted">{intro}</p>}
      <div className="mt-8">{children}</div>
    </section>
  );
}

export default function Home() {
  const link = "text-accent underline-offset-2 hover:underline";
  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-40 border-b border-border bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between gap-4 px-4 sm:px-6">
          <a href="#" className="flex items-center gap-2 font-semibold tracking-tight">
            <span aria-hidden className="grid size-6 place-items-center rounded-md bg-accent font-mono text-xs text-accent-fg">
              t
            </span>
            tacit
          </a>
          <nav aria-label="Sections" className="hidden items-center gap-5 text-sm text-muted md:flex">
            {NAV.map((n) => (
              <a key={n.href} href={n.href} className="hover:text-fg">
                {n.label}
              </a>
            ))}
          </nav>
          <div className="flex items-center gap-1">
            <a
              href={REPO_URL}
              className="grid size-9 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-fg"
              aria-label="Tacit on GitHub"
            >
              <GitHubIcon />
            </a>
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-5xl px-4 sm:px-6">
        {/* Hero */}
        <section aria-labelledby="hero-title" className="pt-16 pb-14 sm:pt-24 sm:pb-20">
          <p className="inline-flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs text-muted">
            Open source · Apache 2.0 · local-first
          </p>
          <h1
            id="hero-title"
            className="mt-5 max-w-3xl text-4xl font-semibold tracking-tight text-balance sm:text-6xl"
          >
            Your team&apos;s memory, wired into every AI tool.
          </h1>
          <p className="mt-5 max-w-2xl text-lg text-muted text-pretty">
            Tacit syncs your team&apos;s GitHub, Slack and Notion into your own Neo4j and serves it
            to Claude and Cursor through a local MCP server. Ask &ldquo;why did we do X, who
            decided, and where&apos;s the evidence?&rdquo; and get answers with citations.
          </p>

          <div className="mt-8 max-w-xl">
            <CodeLine code={INSTALL_CMD} />
            <p className="mt-2 text-xs text-muted">
              Needs Python 3.13 +{" "}
              <a className={link} href="https://docs.astral.sh/uv/">
                uv
              </a>
              . Then run <code className="font-mono">tacit init</code>. No binary to download.
            </p>
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3 text-sm">
            <a
              href={REPO_URL}
              className="inline-flex items-center gap-2 rounded-lg bg-fg px-4 py-2 font-medium text-bg hover:opacity-90"
            >
              <GitHubIcon size={16} /> View on GitHub
            </a>
            <a
              href={NEO4J_COMPOSE}
              className="inline-flex items-center gap-2 rounded-lg border border-border-strong px-4 py-2 font-medium hover:bg-surface-2"
            >
              Self-host Neo4j (Docker Compose)
            </a>
          </div>

          <div className="mt-10">
            <Suspense fallback={<LiveStatsFallback />}>
              <LiveStats />
            </Suspense>
          </div>
        </section>

        <Section
          id="demo"
          eyebrow="Hands-on example"
          title="See what Tacit finds"
          intro={
            <>
              Ask the history of{" "}
              <a className={link} href={DEMO_README}>
                Larkspur Labs
              </a>
              , a made-up startup with three repos and a few decisions they later reversed.
            </>
          }
        >
          <DemoSearch />
        </Section>

        <Section
          id="how"
          eyebrow="How it works"
          title="Runs on your laptops. Stores in your Neo4j."
          intro={
            <>
              One package per teammate, one database per team. Details in the{" "}
              <a className={link} href={DESIGN_DOC}>
                design doc
              </a>
              .
            </>
          }
        >
          <HowItWorks />
        </Section>

        <Section
          id="numbers"
          eyebrow="The numbers"
          title="Fast, free to search, and it finds the evidence"
        >
          <Numbers />
        </Section>

        <Section
          id="trust"
          eyebrow="Trust model"
          title="Simple on purpose"
          intro="Tacit is for small, high-trust teams. Here's exactly what protects your data, and what doesn't."
        >
          <TrustModel />
        </Section>

        <Section
          id="quickstart"
          eyebrow="Quickstart"
          title="From zero to “why did we…?” in six commands"
        >
          <Quickstart />
        </Section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-8 text-sm text-muted sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p>Tacit · Apache 2.0 · No analytics, no cookies, no tracking.</p>
          <nav aria-label="Footer" className="flex flex-wrap gap-4">
            <a href={REPO_URL} className="hover:text-fg">
              GitHub
            </a>
            <a href={`${REPO_URL}#readme`} className="hover:text-fg">
              Docs
            </a>
            <a href={DESIGN_DOC} className="hover:text-fg">
              Design
            </a>
            <a href={`${REPO_URL}/issues`} className="hover:text-fg">
              Issues
            </a>
          </nav>
        </div>
      </footer>
    </>
  );
}
