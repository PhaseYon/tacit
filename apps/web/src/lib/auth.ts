import { betterAuth } from "better-auth";
import { nextCookies } from "better-auth/next-js";
import { jwt, organization } from "better-auth/plugins";
import { Pool } from "pg";

import { sendMail } from "./mailer";

const baseURL = process.env.BETTER_AUTH_URL ?? "http://localhost:3000";

const github =
  process.env.GITHUB_CLIENT_ID && process.env.GITHUB_CLIENT_SECRET
    ? {
        github: {
          clientId: process.env.GITHUB_CLIENT_ID,
          clientSecret: process.env.GITHUB_CLIENT_SECRET,
        },
      }
    : undefined;

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
    nextCookies(), // must be last
  ],
});

export type Session = typeof auth.$Infer.Session;
