import { headers } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { AppHeader } from "@/components/app-header";
import { PageSkeleton } from "@/components/page-skeleton";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/auth";
import { requireSession } from "@/lib/session";

type Result = {
  document_id: string;
  kind: string;
  title: string | null;
  url: string;
  author: string | null;
  created_at: string | null;
  state: string | null;
  container: string;
  snippet: string;
  matched_by: string[];
};

const KIND: Record<string, string> = {
  pull_request: "PR",
  issue: "Issue",
  issue_comment: "Comment",
  pr_review: "Review",
  pr_review_comment: "Code comment",
  markdown_file: "Doc",
};

/** Render **highlighted** fragments from Postgres ts_headline without raw HTML. */
function Snippet({ text }: { text: string }) {
  return (
    <p className="text-sm text-muted-foreground">
      {text.split(/\*\*(.+?)\*\*/g).map((part, i) =>
        i % 2 ? (
          <mark key={i} className="rounded bg-yellow-200/60 px-0.5 text-foreground dark:bg-yellow-500/30">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </p>
  );
}

async function SearchContent({
  params,
  searchParams,
}: {
  params: PageProps<"/w/[slug]/search">["params"];
  searchParams: PageProps<"/w/[slug]/search">["searchParams"];
}) {
  const [{ slug }, query, session] = await Promise.all([params, searchParams, requireSession()]);
  const q = typeof query.q === "string" ? query.q.trim() : "";
  const workspace = await auth.api
    .getFullOrganization({ headers: await headers(), query: { organizationSlug: slug } })
    .catch(() => null);
  if (!workspace) notFound();

  const results = q
    ? (
        await apiFetch<{ results: Result[] }>(
          `/workspaces/${workspace.id}/search?${new URLSearchParams({ q, limit: "20" })}`,
        )
      ).results
    : [];

  return (
    <>
      <AppHeader email={session.user.email}>
        <span className="text-muted-foreground">/</span>
        <Link href={`/w/${slug}`} className="font-medium hover:underline">
          {workspace.name}
        </Link>
      </AppHeader>
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-10">
        <form className="flex gap-2">
          <Input
            name="q"
            defaultValue={q}
            placeholder="Why did we switch to Stripe?"
            autoFocus
            aria-label="Search"
          />
        </form>
        {q && results.length === 0 && (
          <p className="text-muted-foreground">
            No results you have access to. Try other words, or check that your GitHub account is
            linked.
          </p>
        )}
        <ol className="flex flex-col gap-5">
          {results.map((r) => (
            <li key={r.document_id} className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="secondary">{KIND[r.kind] ?? r.kind}</Badge>
                <span>{r.container}</span>
                {r.author && <span>· {r.author}</span>}
                {r.created_at && <span>· {new Date(r.created_at).toLocaleDateString()}</span>}
                {r.state && <span>· {r.state}</span>}
              </div>
              <a
                href={r.url}
                target="_blank"
                rel="noreferrer"
                className="font-medium text-primary hover:underline"
              >
                {r.title ?? r.url}
              </a>
              <Snippet text={r.snippet} />
            </li>
          ))}
        </ol>
      </main>
    </>
  );
}

export default function SearchPage({ params, searchParams }: PageProps<"/w/[slug]/search">) {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <SearchContent params={params} searchParams={searchParams} />
    </Suspense>
  );
}
