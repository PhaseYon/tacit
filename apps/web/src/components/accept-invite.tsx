"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { authClient } from "@/lib/auth-client";

export function AcceptInvite({ invitationId, slug }: { invitationId: string; slug: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  return (
    <div className="flex gap-2">
      <Button
        disabled={pending}
        onClick={async () => {
          setPending(true);
          const { error } = await authClient.organization.acceptInvitation({ invitationId });
          setPending(false);
          if (error) {
            toast.error(error.message ?? "Could not accept invite");
            return;
          }
          router.push(`/w/${slug}`);
        }}
      >
        {pending ? "Joining…" : "Accept invite"}
      </Button>
      <Button
        variant="outline"
        disabled={pending}
        onClick={async () => {
          await authClient.organization.rejectInvitation({ invitationId });
          router.push("/dashboard");
        }}
      >
        Decline
      </Button>
    </div>
  );
}
