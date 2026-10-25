"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { authClient } from "@/lib/auth-client";

/** The oauthProviderClient plugin attaches the signed oauth_query automatically. */
export function ConsentButtons({ scope }: { scope: string }) {
  const [pending, setPending] = useState(false);

  async function decide(accept: boolean) {
    setPending(true);
    const { data, error } = await authClient.oauth2.consent({ accept, scope });
    if (error) {
      setPending(false);
      toast.error(error.message ?? "Something went wrong");
      return;
    }
    const target = (data as { url?: string; redirect_uri?: string } | null)?.url ??
      (data as { redirect_uri?: string } | null)?.redirect_uri;
    if (target) window.location.href = target;
  }

  return (
    <div className="flex gap-2">
      <Button disabled={pending} onClick={() => decide(true)}>
        Allow
      </Button>
      <Button variant="outline" disabled={pending} onClick={() => decide(false)}>
        Deny
      </Button>
    </div>
  );
}
