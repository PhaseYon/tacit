"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { signOut } from "@/lib/auth-client";

export function AppHeader({ email, children }: { email: string; children?: React.ReactNode }) {
  const router = useRouter();
  return (
    <header className="flex h-14 items-center gap-4 border-b px-4 sm:px-6">
      <Link href="/dashboard" className="font-semibold tracking-tight">
        Tacit
      </Link>
      {children}
      <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground">
        <span className="hidden sm:inline">{email}</span>
        <Button
          variant="ghost"
          size="sm"
          onClick={async () => {
            await signOut();
            router.push("/");
            router.refresh();
          }}
        >
          Sign out
        </Button>
      </div>
    </header>
  );
}
