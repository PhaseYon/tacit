import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { AppHeader } from "@/components/app-header";
import { GitHubSection } from "@/components/github-section";
import { InviteForm } from "@/components/invite-form";
import { PageSkeleton } from "@/components/page-skeleton";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { type ApiMember, apiFetch } from "@/lib/api";
import { auth } from "@/lib/auth";
import { requireSession } from "@/lib/session";

async function WorkspacePageContent({
  params,
  searchParams,
}: {
  params: PageProps<"/w/[slug]">["params"];
  searchParams: PageProps<"/w/[slug]">["searchParams"];
}) {
  const { slug } = await params;
  const query = await searchParams;
  const session = await requireSession();

  const workspace = await auth.api
    .getFullOrganization({ headers: await headers(), query: { organizationSlug: slug } })
    .catch(() => null);
  if (!workspace) notFound();

  // Members come from FastAPI on purpose: it exercises the JWT -> JWKS -> RLS path.
  const members = await apiFetch<ApiMember[]>(`/workspaces/${workspace.id}/members`);
  const myRole = members.find((m) => m.user_id === session.user.id)?.role;
  const canInvite = myRole === "owner" || myRole === "admin";
  const pending = workspace.invitations.filter((i) => i.status === "pending");

  return (
    <>
      <AppHeader email={session.user.email}>
        <span className="text-muted-foreground">/</span>
        <span className="font-medium">{workspace.name}</span>
      </AppHeader>
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10">
        <Card>
          <CardHeader>
            <CardTitle>Members</CardTitle>
            <CardDescription>{members.length} in this workspace</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead className="text-right">Role</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.map((m) => (
                  <TableRow key={m.user_id}>
                    <TableCell>{m.name}</TableCell>
                    <TableCell className="text-muted-foreground">{m.email}</TableCell>
                    <TableCell className="text-right">
                      <Badge variant={m.role === "owner" ? "default" : "secondary"}>{m.role}</Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <GitHubSection
          workspaceId={workspace.id}
          slug={slug}
          canManage={canInvite}
          flash={{
            connected: query.github === "connected",
            requested: query.github === "requested",
            error: typeof query.github_error === "string" ? query.github_error : undefined,
          }}
        />

        {canInvite && (
          <Card>
            <CardHeader>
              <CardTitle>Invite people</CardTitle>
              <CardDescription>They&apos;ll get an email with a link to join.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <InviteForm workspaceId={workspace.id} />
              {pending.length > 0 && (
                <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
                  {pending.map((i) => (
                    <li key={i.id}>
                      {i.email} · {i.role} · pending
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        )}
      </main>
    </>
  );
}

// Session and data are per-request, so they stream in behind Suspense.
export default function WorkspacePage({ params, searchParams }: PageProps<"/w/[slug]">) {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <WorkspacePageContent params={params} searchParams={searchParams} />
    </Suspense>
  );
}
