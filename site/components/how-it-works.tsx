// Simplified version of the "after" diagram in docs/design/local-first.md §2.

function Arrow({ label, up = false }: { label: string; up?: boolean }) {
  return (
    <div className="flex items-center justify-center gap-3 py-2 text-xs text-muted">
      <svg
        aria-hidden
        width="14"
        height="40"
        viewBox="0 0 14 40"
        className={up ? "rotate-180" : undefined}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path d="M7 1v36M1.5 31.5 7 37l5.5-5.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span>{label}</span>
    </div>
  );
}

function Chip({ children, accent = false }: { children: React.ReactNode; accent?: boolean }) {
  return (
    <span
      className={
        "rounded-md border px-2 py-1 font-mono text-xs " +
        (accent
          ? "border-accent/40 bg-accent-soft text-fg"
          : "border-border bg-surface-2 text-fg")
      }
    >
      {children}
    </span>
  );
}

function Laptop({ who, className = "" }: { who: string; className?: string }) {
  return (
    <div className={"rounded-xl border border-border bg-surface p-4 " + className}>
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <svg aria-hidden width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-muted">
          <rect x="3" y="4" width="18" height="12" rx="1.5" />
          <path d="M2 20h20" />
        </svg>
        {who}
      </div>
      <div className="flex flex-col items-start gap-1.5 text-xs">
        <Chip>Claude · Cursor</Chip>
        <span className="pl-2 text-muted">↓ stdio</span>
        <Chip accent>tacit mcp</Chip>
        <Chip accent>tacit sync</Chip>
        <span className="mt-1 text-muted">every query logged first</span>
      </div>
    </div>
  );
}

export function HowItWorks() {
  return (
    <figure
      className="rounded-2xl border border-border bg-bg p-4 sm:p-6"
      aria-labelledby="hiw-caption"
    >
      {/* Sources */}
      <div className="rounded-xl border border-dashed border-border-strong p-4 text-center">
        <div className="flex flex-wrap justify-center gap-2">
          <Chip>GitHub</Chip>
          <Chip>Slack</Chip>
          <Chip>Notion</Chip>
        </div>
        <p className="mt-2 text-xs text-muted">Only what&apos;s in your team&apos;s sync scope</p>
      </div>

      <Arrow up label="polled with each teammate's own token" />

      {/* Laptops */}
      <div className="grid gap-3 md:grid-cols-3">
        <Laptop who="Your laptop" />
        <Laptop who="Teammate's laptop" className="hidden md:block" />
        <Laptop who="Teammate's laptop" className="hidden md:block" />
      </div>
      <p className="mt-2 text-center text-xs text-muted md:hidden">…one per teammate</p>

      <Arrow label="one URL + password per team" />

      {/* Team's Neo4j */}
      <div className="rounded-xl border-2 border-accent/50 bg-accent-soft/50 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="font-medium">Your team&apos;s Neo4j</p>
          <p className="text-xs text-muted">Aura Free · Docker on a VPS</p>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Chip>chunk index (keyword + vector)</Chip>
          <Chip>graph facts over time (optional)</Chip>
          <Chip>scope · cursors · access log</Chip>
        </div>
      </div>

      <figcaption id="hiw-caption" className="mt-5 text-sm text-muted">
        <strong className="font-semibold text-fg">We host no team data.</strong> Sync, search and
        the MCP server run on each laptop; the only shared piece is a Neo4j your team runs. The
        only things we run are this website and a demo database with fictional data.
      </figcaption>
    </figure>
  );
}
