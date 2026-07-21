import { betterAuth } from "better-auth";
import { apiKey } from "@better-auth/api-key";
import Database from "better-sqlite3";

// better-auth instance shared by the gateway server, the seed script and the
// `better-auth` migration CLI. Configuration comes entirely from the
// environment so the same file works for provisioning and at runtime.
const dbPath = process.env.AUTH_DB || "/var/lib/mcp-auth/auth.db";

// WAL + a busy timeout let the seed script and the running gateway touch the DB
// concurrently without tripping over each other with SQLITE_BUSY.
const db = new Database(dbPath);
db.pragma("journal_mode = WAL");
db.pragma("busy_timeout = 5000");

export const auth = betterAuth({
    database: db,
    // Placeholder is only used by the migration CLI (schema is secret-agnostic);
    // the running service always gets the real secret from /etc/sandbox/gateway.env.
    secret: process.env.BETTER_AUTH_SECRET || "migration-placeholder-secret",
    baseURL: process.env.BETTER_AUTH_URL || "http://127.0.0.1:8080",
    // Email+password only exists so the seed script can own the API keys. It is
    // NOT reachable from the network: server.mjs never mounts `auth.handler`, so
    // there is no public /api/auth/* route. disableSignUp must stay false because
    // the seed uses the server-side signUpEmail API (which the flag also blocks).
    emailAndPassword: {
        enabled: true,
        disableSignUp: false,
    },
    plugins: [
        apiKey({
            // Keys are validated explicitly in server.mjs (verifyApiKey) so we can
            // accept both `x-api-key` and `Authorization: Bearer <key>`.
            defaultKeyLength: 48,
            // The single gateway key proxies every MCP request (including each SSE
            // message POST); per-key rate limiting would throttle a normal session,
            // so disable it here — the network edge is the place for rate limiting.
            rateLimit: { enabled: false },
        }),
    ],
});
