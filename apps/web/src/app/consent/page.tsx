import { verifyOAuthQueryParams } from "@better-auth/oauth-provider";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { Suspense } from "react";

import { ConsentButtons } from "@/components/consent-buttons";
import { PageSkeleton } from "@/components/page-skeleton";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { auth } from "@/lib/auth";
import { getSession } from "@/lib/session";

const SCOPE_TEXT: Record<string, string> = {
  openid: "Know who you are",
  profile: "See your name and picture",
  email: "See your email address",
  offline_access: "Stay connected without asking again (refresh tokens)",
};

async function ConsentContent({ searchParams }: { searchParams: PageProps<"/consent">["searchParams"] }) {
  const params = await searchParams;
  const query = new URLSearchParams(
    Object.entries(params).flatMap(([k, v]) => (Array.isArray(v) ? v.map((x) => [k, x]) : v ? [[k, v]] : [])),
  ).toString();

  const session = await getSession();
  if (!session) redirect(`/sign-in?${query}`);

  // The authorization server signs these params; never render an unverified request.
  const { secret } = await auth.$context;
  if (!(await verifyOAuthQueryParams(query, secret))) {
    return <p className="text-sm text-destructive">This authorization request is invalid or expired.</p>;
  }

  const clientId = String(params.client_id ?? "");
  const scopes = String(params.scope ?? "").split(" ").filter(Boolean);
  const client = await auth.api
    .getOAuthClientPublic({ headers: await headers(), query: { client_id: clientId } })
    .catch(() => null);
  const name = (client as { client_name?: string } | null)?.client_name ?? "An application";

  return (
    <Card className="w-full max-w-md">
      <CardHeader>
        <CardTitle>{name} wants to access Tacit</CardTitle>
        <CardDescription>
          Signed in as {session.user.email}. It will be able to search your team&apos;s knowledge
          with your permissions, so it sees only what you can see.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <ul className="list-disc pl-5 text-sm text-muted-foreground">
          <li>Search and read workspace knowledge you have access to</li>
          {scopes.map((s) => (
            <li key={s}>{SCOPE_TEXT[s] ?? s}</li>
          ))}
        </ul>
        <ConsentButtons scope={scopes.join(" ")} />
      </CardContent>
    </Card>
  );
}

export default function ConsentPage({ searchParams }: PageProps<"/consent">) {
  return (
    <main className="flex flex-1 items-center justify-center px-4 py-12">
      <Suspense fallback={<PageSkeleton />}>
        <ConsentContent searchParams={searchParams} />
      </Suspense>
    </main>
  );
}
