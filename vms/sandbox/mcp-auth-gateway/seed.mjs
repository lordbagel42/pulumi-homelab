import fs from "node:fs";
import { auth } from "./auth.js";

// Provisions exactly one API key for the MCP gateway on first run and writes the
// plaintext to a root-only file for the operator to hand to Poke. Idempotent: if
// the key file already has a value we leave everything untouched (better-auth
// only ever exposes the plaintext at creation time).
const KEY_FILE = process.env.API_KEY_FILE || "/etc/sandbox/mcp-api-key";
const email = process.env.GATEWAY_ADMIN_EMAIL || "poke@sandbox.local";
const password = process.env.GATEWAY_ADMIN_PASSWORD;

if (!password) {
    console.error("GATEWAY_ADMIN_PASSWORD is not set; refusing to seed.");
    process.exit(1);
}

if (fs.existsSync(KEY_FILE) && fs.readFileSync(KEY_FILE, "utf8").trim()) {
    console.log(`API key already provisioned at ${KEY_FILE}; nothing to do.`);
    process.exit(0);
}

async function ensureUser() {
    try {
        const res = await auth.api.signUpEmail({
            body: { email, password, name: "poke" },
        });
        return res.user.id;
    } catch {
        // Already exists — sign in to recover the user id.
        const res = await auth.api.signInEmail({ body: { email, password } });
        return res.user.id;
    }
}

const userId = await ensureUser();

const created = await auth.api.createApiKey({
    body: {
        name: "poke-sandbox",
        userId,
        prefix: "poke_",
    },
});

fs.writeFileSync(KEY_FILE, `${created.key}\n`, { mode: 0o600 });
console.log(`Provisioned MCP API key at ${KEY_FILE}`);
process.exit(0);
