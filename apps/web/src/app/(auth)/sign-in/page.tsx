import { redirect } from "next/navigation";
import { Suspense } from "react";

import { AuthForm } from "@/components/auth-form";
import { getSession, githubLoginEnabled } from "@/lib/session";

export default async function Page() {
  if (await getSession()) redirect("/dashboard");
  return (
    <Suspense>
      <AuthForm mode="sign-in" githubEnabled={githubLoginEnabled} />
    </Suspense>
  );
}
