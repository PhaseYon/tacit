import { redirect } from "next/navigation";
import { Suspense } from "react";

import { AuthForm } from "@/components/auth-form";
import { getSession, githubLoginEnabled } from "@/lib/session";

async function AuthPageContent() {
  if (await getSession()) redirect("/dashboard");
  return <AuthForm mode="sign-in" githubEnabled={githubLoginEnabled} />;
}

export default function Page() {
  return (
    <Suspense>
      <AuthPageContent />
    </Suspense>
  );
}
