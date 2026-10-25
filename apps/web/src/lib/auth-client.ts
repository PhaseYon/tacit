"use client";

import { oauthProviderClient } from "@better-auth/oauth-provider/client";
import { jwtClient, organizationClient } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";

export const authClient = createAuthClient({
  plugins: [organizationClient(), jwtClient(), oauthProviderClient()],
});

export const { signIn, signUp, signOut, useSession } = authClient;
