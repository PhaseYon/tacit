import { headers } from "next/headers";
import Link from "next/link";
import { Suspense } from "react";

import { AcceptInvite } from "@/components/accept-invite";
import { PageSkeleton } from "@/components/page-skeleton";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { auth } from "@/lib/auth";
import { getSession } from "@/lib/session";

async function InvitePageContent({ params }: { params: PageProps<"/invite/[id]">["params"] }) {
  const { id } = await params;
  const session = await getSession();
  const next = encodeURIComponent(`/invite/${id}`);

  let body: React.ReactNode;
  let title = "You've been invited";
  if (!session) {
    body = (
      <div className="flex gap-2">
        <Link href={`/sign-up?next=${next}`} className={buttonVariants()}>
          Create account
        </Link>
        <Link href={`/sign-in?next=${next}`} className={buttonVariants({ variant: "outline" })}>
          Sign in
        </Link>
      </div>
    );
  } else {
    // Better Auth only returns the invite if it was sent to this user's email.
    const invite = await auth.api
      .getInvitation({ headers: await headers(), query: { id } })
      .catch(() => null);
    if (!invite || invite.status !== "pending") {
      title = "Invite not available";
      body = (
        <p className="text-sm text-muted-foreground">
          This invite has expired, was already used, or was sent to a different email than{" "}
          {session.user.email}.
        </p>
      );
    } else {
      title = `Join ${invite.organizationName}`;
      body = <AcceptInvite invitationId={id} slug={invite.organizationSlug} />;
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center px-4 py-12">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{title}</CardTitle>
          <CardDescription>Tacit workspace invitation</CardDescription>
        </CardHeader>
        <CardContent>{body}</CardContent>
      </Card>
    </main>
  );
}

// Session and data are per-request, so they stream in behind Suspense.
export default function InvitePage({ params }: PageProps<"/invite/[id]">) {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <InvitePageContent params={params} />
    </Suspense>
  );
}
