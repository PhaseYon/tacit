import Link from "next/link";
import { redirect } from "next/navigation";

import { buttonVariants } from "@/components/ui/button";
import { getSession } from "@/lib/session";

export default async function Home() {
  if (await getSession()) redirect("/dashboard");

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 px-4 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">Tacit</h1>
      <p className="max-w-md text-muted-foreground">
        Your team&apos;s memory, wired into every AI tool. Ask why something was done, who
        decided, and see the evidence.
      </p>
      <div className="flex gap-3">
        <Link href="/sign-up" className={buttonVariants()}>
          Get started
        </Link>
        <Link href="/sign-in" className={buttonVariants({ variant: "outline" })}>
          Sign in
        </Link>
      </div>
    </main>
  );
}
