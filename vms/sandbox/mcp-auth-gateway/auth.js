import { betterAuth } from "better-auth";
import { apiKey } from "@better-auth/api-key";
import Database from "better-sqlite3";

// better-auth instance shared by the gateway server, the seed script and the
// `better-auth` migration CLI. Configuration comes entirely from the
// environment so the same file works for provisioning and at runtime.
const dbPath = process.env.AUTH_DB || "/var/lib/mcp-auth/auth.db";

export const auth = betterAuth({
    database: new Database(dbPath),
    // Placeholder is only used by the migration CLI (schema is secret-agnostic);
    // the running service always gets the real secret from /etc/sandbox/gateway.env.
    secret: process.env.BETTER_AUTH_SECRET || "migration-placeholder-secret",
    baseURL: process.env.BETTER_AUTH_URL || "http://127.0.0.1:8080",
    // Email+password only exists to own the API keys — there is no public sign-up.
    emailAndPassword: {
        enabled: true,
        disableSignUp: false,
    },
    plugins: [
        apiKey({
            // Keys are validated explicitly in server.mjs (verifyApiKey) so we can
            // accept both `x-api-key` and `Authorization: Bearer <key>`.
            defaultKeyLength: 48,
        }),
    ],
});
