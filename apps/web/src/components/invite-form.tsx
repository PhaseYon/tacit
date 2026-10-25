"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { authClient } from "@/lib/auth-client";

export function InviteForm({ workspaceId }: { workspaceId: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formEl = e.currentTarget;
    const form = new FormData(formEl);
    setPending(true);
    const { error } = await authClient.organization.inviteMember({
      organizationId: workspaceId,
      email: String(form.get("email")),
      role: form.get("role") === "admin" ? "admin" : "member",
    });
    setPending(false);
    if (error) {
      toast.error(error.message ?? "Could not send invite");
      return;
    }
    toast.success("Invite sent");
    formEl.reset();
    router.refresh();
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-2 sm:flex-row">
      <Input name="email" type="email" placeholder="teammate@company.com" required />
      <select
        name="role"
        defaultValue="member"
        className="h-8 rounded-lg border bg-background px-2 text-sm"
        aria-label="Role"
      >
        <option value="member">Member</option>
        <option value="admin">Admin</option>
      </select>
      <Button type="submit" disabled={pending}>
        {pending ? "Sending…" : "Invite"}
      </Button>
    </form>
  );
}
