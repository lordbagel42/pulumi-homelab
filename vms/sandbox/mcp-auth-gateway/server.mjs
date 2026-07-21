import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { Agent } from "undici";
import { auth } from "./auth.js";

// ── Config ───────────────────────────────────────────────────────────────────
const PORT = Number(process.env.PORT || 8080);

// SSE connections are long-lived and can idle between events; undici's default
// body/headers timeouts (~300s) would abort them. Disable them for proxied calls.
const proxyDispatcher = new Agent({ bodyTimeout: 0, headersTimeout: 0 });

// Hop-by-hop headers must not be forwarded verbatim (RFC 7230 §6.1) — they
// describe a single connection and confuse the receiving HTTP layer's framing.
const HOP_BY_HOP = new Set([
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
]);

// Public path prefix → internal supergateway URL. The MCP servers listen only on
// localhost (and are firewalled off); this gateway is the sole network entrypoint.
const UPSTREAMS = {
    "claude-code": process.env.CLAUDE_UPSTREAM || "http://127.0.0.1:9100",
    "filesystem": process.env.FS_UPSTREAM || "http://127.0.0.1:9101",
};

const app = new Hono();

// NOTE: better-auth's HTTP endpoints (/api/auth/*) are deliberately NOT exposed.
// Doing so would publish the API-key creation + sign-up routes, letting anyone
// mint a valid key. Keys are managed in-process only (see seed.mjs, run over SSH),
// and verification below uses the in-process server API.

app.get("/healthz", (c) => c.text("ok"));

// ── API-key extraction + verification ──────────────────────────────────────────
function extractKey(req) {
    const headerKey = req.header("x-api-key");
    if (headerKey) return headerKey.trim();

    const authorization = req.header("authorization");
    if (authorization && /^Bearer\s+/i.test(authorization)) {
        return authorization.replace(/^Bearer\s+/i, "").trim();
    }
    return null;
}

async function isAuthorized(c) {
    const key = extractKey(c.req);
    if (!key) return false;
    try {
        const result = await auth.api.verifyApiKey({ body: { key } });
        return Boolean(result && result.valid);
    } catch {
        return false;
    }
}

// ── Authenticated reverse proxy to the supergateway SSE endpoints ───────────────
async function handleProxy(c, prefix, upstream) {
    if (!(await isAuthorized(c))) {
        return c.json(
            { error: "unauthorized", error_description: "Missing or invalid API key" },
            401,
            { "WWW-Authenticate": 'Bearer realm="mcp", error="invalid_token"' },
        );
    }

    const url = new URL(c.req.url);
    const path = url.pathname.slice(`/${prefix}`.length) || "/";
    const target = upstream + path + url.search;

    const headers = new Headers(c.req.raw.headers);
    headers.delete("host");
    // Don't forward the credential to the upstream MCP server.
    headers.delete("authorization");
    headers.delete("x-api-key");

    const init = { method: c.req.method, headers, redirect: "manual", dispatcher: proxyDispatcher };
    if (c.req.method !== "GET" && c.req.method !== "HEAD") {
        init.body = c.req.raw.body;
        init.duplex = "half";
    }

    const upstreamResponse = await fetch(target, init);
    const responseHeaders = new Headers();
    for (const [k, v] of upstreamResponse.headers) {
        if (!HOP_BY_HOP.has(k.toLowerCase())) responseHeaders.set(k, v);
    }
    // Stream the (possibly long-lived SSE) body straight back to the client.
    return new Response(upstreamResponse.body, {
        status: upstreamResponse.status,
        headers: responseHeaders,
    });
}

for (const [prefix, upstream] of Object.entries(UPSTREAMS)) {
    app.all(`/${prefix}`, (c) => handleProxy(c, prefix, upstream));
    app.all(`/${prefix}/*`, (c) => handleProxy(c, prefix, upstream));
}

serve({ fetch: app.fetch, port: PORT, hostname: "0.0.0.0" }, (info) => {
    console.log(`mcp-auth-gateway listening on :${info.port}`);
    for (const [prefix, upstream] of Object.entries(UPSTREAMS)) {
        console.log(`  /${prefix}/* → ${upstream}`);
    }
});
