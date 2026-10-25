import { mcp } from "@better-auth/mcp";
import { betterAuth } from "better-auth";
import { nextCookies } from "better-auth/next-js";
import { genericOAuth, jwt, organization } from "better-auth/plugins";
import { Pool } from "pg";

import { sendMail } from "./mailer";

const baseURL = process.env.BETTER_AUTH_URL ?? "http://localhost:3000";
// The MCP server (FastAPI) is the OAuth protected resource; tokens are bound to it.
const mcpResource = process.env.MCP_RESOURCE_URL ?? "http://localhost:8000/mcp";

// GitHub sign-in/linking uses the Tacit GitHub App's OAuth credentials, so a linked
// account is an OAuth-verified GitHub identity (used for repo permission sync).
const githubClientId = process.env.GITHUB_APP_CLIENT_ID;
const githubClientSecret = process.env.GITHUB_APP_CLIENT_SECRET;
const github =
  githubClientId && githubClientSecret
    ? { github: { clientId: githubClientId, clientSecret: githubClientSecret } }
    : undefined;

// "Sign in with Slack" (OpenID Connect) through genericOAuth rather than the built-in
// Slack provider: the built-in one stores only the Slack user id, but Slack ids are
// unique per team, so the account id here is "<team_id>:<user_id>" from Slack's verified
// userInfo claims. The API matches it against channel membership for permission sync.
const slackClientId = process.env.SLACK_CLIENT_ID;
const slackClientSecret = process.env.SLACK_CLIENT_SECRET;
type SlackClaims = Record<string, unknown> & { sub?: string };
const slackPlugins =
  slackClientId && slackClientSecret
    ? [
        genericOAuth({
          config: [
            {
              providerId: "slack",
              name: "Slack",
              clientId: slackClientId,
              clientSecret: slackClientSecret,
              authorizationUrl: "https://slack.com/openid/connect/authorize",
              tokenUrl: "https://slack.com/api/openid.connect.token",
              userInfoUrl: "https://slack.com/api/openid.connect.userInfo",
              scopes: ["openid", "profile", "email"],
              pkce: true,
              getUserInfo: async (tokens) => {
                const res = await fetch("https://slack.com/api/openid.connect.userInfo", {
                  headers: { Authorization: `Bearer ${tokens.accessToken}` },
                });
                const p = (await res.json()) as SlackClaims & { ok?: boolean };
                if (!res.ok || !p.ok) return null;
                return {
                  ...p,
                  id: p.sub,
                  email: p.email as string | undefined,
                  emailVerified: Boolean(p.email_verified),
                  name: (p.name as string | undefined) ?? "",
                  image: p.picture as string | undefined,
                };
              },
              // Empty claims make Better Auth reject the link (fail closed).
              accountSubject: ({ profile }) => {
                const team = profile["https://slack.com/team_id"];
                const user = profile["https://slack.com/user_id"];
                return typeof team === "string" && typeof user === "string" ? `${team}:${user}` : "";
              },
            },
          ],
        }),
      ]
    : [];

export const auth = betterAuth({
  baseURL,
  // Connects as tacit_auth, which can only reach auth tables. Alembic owns the schema.
  database: new Pool({ connectionString: process.env.AUTH_DATABASE_URL }),
  emailAndPassword: { enabled: true },
  emailVerification: {
    sendOnSignUp: true,
    sendVerificationEmail: async ({ user, url }) => {
      await sendMail({
        to: user.email,
        subject: "Verify your Tacit email",
        text: `Confirm your email: ${url}`,
      });
    },
  },
  socialProviders: github,
  account: {
    accountLinking: {
      // People's GitHub email often differs from their work email. Linking happens
      // from a signed-in session via OAuth, so the user proves control of both.
      allowDifferentEmails: true,
    },
  },
  plugins: [
    // Workspaces are Better Auth organizations.
    organization({
      invitationExpiresIn: 60 * 60 * 24 * 7,
      sendInvitationEmail: async ({ email, id, organization, inviter }) => {
        const url = `${baseURL}/invite/${id}`;
        await sendMail({
          to: email,
          subject: `${inviter.user.name} invited you to ${organization.name} on Tacit`,
          text: `Join ${organization.name} on Tacit: ${url}`,
        });
      },
    }),
    // Short-lived JWTs for FastAPI, which verifies them via /api/auth/jwks.
    jwt({
      jwt: {
        issuer: process.env.AUTH_JWT_ISSUER ?? baseURL,
        audience: process.env.AUTH_JWT_AUDIENCE ?? "tacit-api",
        expirationTime: "15m",
        definePayload: ({ user }) => ({ email: user.email, name: user.name }),
      },
    }),
    // OAuth 2.1 authorization server for MCP clients (Claude, Cursor). Access tokens
    // are JWTs with aud = the MCP resource, so they can't be used against the REST
    // API (aud = tacit-api), and web-session tokens can't be used against MCP.
    mcp({
      resource: mcpResource,
      loginPage: "/sign-in",
      consentPage: "/consent",
      scopes: ["openid", "profile", "email", "offline_access"],
      // Most MCP clients still register dynamically (RFC 7591); public clients use PKCE.
      allowDynamicClientRegistration: true,
      allowUnauthenticatedClientRegistration: true,
      clientRegistrationDefaultApplicationType: "infer",
      accessTokenExpiresIn: 60 * 15,
    }),
    ...slackPlugins,
    nextCookies(), // must be last
  ],
});

export type Session = typeof auth.$Infer.Session;
