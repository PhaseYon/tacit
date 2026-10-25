import { headers } from "next/headers";
import Link from "next/link";
import { Suspense } from "react";

import { AppHeader } from "@/components/app-header";
import { CreateWorkspaceForm } from "@/components/create-workspace-form";
import { PageSkeleton } from "@/components/page-skeleton";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { auth } from "@/lib/auth";
import { requireSession } from "@/lib/session";

async function DashboardPageContent() {
  const session = await requireSession();
  const workspaces = await auth.api.listOrganizations({ headers: await headers() });

  return (
    <>
      <AppHeader email={session.user.email} />
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10">
        <section className="flex flex-col gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">Your workspaces</h1>
          {workspaces.length === 0 ? (
            <p className="text-muted-foreground">
              You&apos;re not in any workspace yet. Create one, or ask a teammate for an invite.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {workspaces.map((w) => (
                <Link key={w.id} href={`/w/${w.slug}`}>
                  <Card className="transition-colors hover:bg-muted/50">
                    <CardHeader>
                      <CardTitle>{w.name}</CardTitle>
                      <CardDescription>{w.slug}</CardDescription>
                    </CardHeader>
                  </Card>
                </Link>
              ))}
            </div>
          )}
        </section>
        <Card>
          <CardHeader>
            <CardTitle>New workspace</CardTitle>
            <CardDescription>One workspace per team. You can invite people next.</CardDescription>
          </CardHeader>
          <CardContent>
            <CreateWorkspaceForm />
          </CardContent>
        </Card>
      </main>
    </>
  );
}

// Session and data are per-request, so they stream in behind Suspense.
export default function DashboardPage() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <DashboardPageContent />
    </Suspense>
  );
}
