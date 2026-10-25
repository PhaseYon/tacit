"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { signIn, signUp } from "@/lib/auth-client";

/** Only allow same-site relative redirects. */
function safeNext(next: string | null) {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
}

export function AuthForm({ mode, githubEnabled }: { mode: "sign-in" | "sign-up"; githubEnabled: boolean }) {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const email = String(form.get("email"));
    const password = String(form.get("password"));
    setPending(true);
    const { error } =
      mode === "sign-up"
        ? await signUp.email({ email, password, name: String(form.get("name")) })
        : await signIn.email({ email, password });
    setPending(false);
    if (error) {
      toast.error(error.message ?? "Something went wrong");
      return;
    }
    router.push(next);
    router.refresh();
  }

  const isSignUp = mode === "sign-up";
  const otherHref = `${isSignUp ? "/sign-in" : "/sign-up"}${next !== "/dashboard" ? `?next=${encodeURIComponent(next)}` : ""}`;

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>{isSignUp ? "Create your account" : "Sign in to Tacit"}</CardTitle>
        <CardDescription>
          {isSignUp ? "Already have an account? " : "New here? "}
          <Link href={otherHref} className="underline underline-offset-4">
            {isSignUp ? "Sign in" : "Create an account"}
          </Link>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {githubEnabled && (
          <>
            <Button
              variant="outline"
              onClick={() => signIn.social({ provider: "github", callbackURL: next })}
            >
              Continue with GitHub
            </Button>
            <Separator />
          </>
        )}
        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          {isSignUp && (
            <div className="grid gap-1.5">
              <Label htmlFor="name">Name</Label>
              <Input id="name" name="name" required autoComplete="name" />
            </div>
          )}
          <div className="grid gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input id="email" name="email" type="email" required autoComplete="email" />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              name="password"
              type="password"
              required
              minLength={8}
              autoComplete={isSignUp ? "new-password" : "current-password"}
            />
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Please wait…" : isSignUp ? "Create account" : "Sign in"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
