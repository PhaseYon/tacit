// RFC 8414 metadata for the OAuth authorization server (issuer = site root).
// MCP clients discover it from the MCP server's protected-resource metadata.
import { oauthProviderAuthServerMetadata } from "@better-auth/oauth-provider";

import { auth } from "@/lib/auth";

export const GET = oauthProviderAuthServerMetadata(auth);
