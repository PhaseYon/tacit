"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { authClient } from "@/lib/auth-client";

function slugify(name: string) {
  const base = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  // Short random suffix so two teams with the same name don't collide.
  return `${base || "workspace"}-${Math.random().toString(36).slice(2, 6)}`;
}

export function CreateWorkspaceForm() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const name = String(new FormData(e.currentTarget).get("name")).trim();
    if (!name) return;
    setPending(true);
    const slug = slugify(name);
    const { error } = await authClient.organization.create({ name, slug });
    setPending(false);
    if (error) {
      toast.error(error.message ?? "Could not create workspace");
      return;
    }
    router.push(`/w/${slug}`);
  }

  return (
    <form onSubmit={onSubmit} className="flex gap-2">
      <Input name="name" placeholder="Workspace name, e.g. Acme Eng" required maxLength={80} />
      <Button type="submit" disabled={pending}>
        {pending ? "Creating…" : "Create"}
      </Button>
    </form>
  );
}
