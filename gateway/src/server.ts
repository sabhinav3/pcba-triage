// Gateway :8787 — one origin for the dashboard.
//   /api/*                      -> Python API (TRIAGE_API_URL, default :8000)
//   POST /socket/:actor/:id     -> WebSocket grant for BoardSession | BenchmarkRun (durable-actors prepareWebsocket)
//   POST /bench/:runId/begin    -> BenchmarkRun.begin(...) then resume() in the background
//   GET  /health                -> api + actors reachability
//   anything else               -> web/dist (built dashboard), if present
// The Supabase service-role key is never read here; it stays in the Python API process.
import { existsSync } from "node:fs"
import { join, normalize } from "node:path"

import { actors } from "../generated/index.js"

const PORT = Number(process.env.GATEWAY_PORT ?? 8787)
const API = process.env.TRIAGE_API_URL ?? "http://127.0.0.1:8000"
const CONTROL = process.env.DURABLE_ACTORS_CONTROL_PLANE_URL ?? "http://127.0.0.1:7100"
const WEB_DIST = normalize(join(import.meta.dir, "..", "..", "web", "dist"))
const ID_RE = /^[A-Za-z0-9._-]+$/

const cors = {
    "access-control-allow-origin": process.env.CORS_ORIGIN ?? "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type"
}

function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...cors } })
}

async function reachable(url: string): Promise<boolean> {
    try {
        const r = await fetch(url, { signal: AbortSignal.timeout(1500) })
        return r.status < 500
    } catch {
        return false
    }
}

async function grant(actor: string, id: string): Promise<Response> {
    if (!ID_RE.test(id)) return json({ error: "bad actor id" }, 400)
    try {
        if (actor === "BoardSession") return json(await actors.BoardSession.prepareWebsocket({ actorId: id, metadata: null }))
        if (actor === "BenchmarkRun") return json(await actors.BenchmarkRun.prepareWebsocket({ actorId: id, metadata: null }))
        return json({ error: `unknown actor ${actor}` }, 404)
    } catch (e) {
        return json({ error: `actors unavailable at ${CONTROL}: ${e instanceof Error ? e.message : String(e)}` }, 503)
    }
}

type BeginBody = { board_id?: string; methods?: ("none" | "rules" | "agent")[]; case_ids?: string[] | null }

async function beginBench(runId: string, body: BeginBody): Promise<Response> {
    if (!ID_RE.test(runId)) return json({ error: "bad run id" }, 400)
    const stub = actors.BenchmarkRun.get(runId)
    const state = await stub.begin(body.board_id ?? "rp2040_easyduino", body.methods ?? ["none", "rules"], body.case_ids ?? null)
    void stub.resume().catch(e => console.error(`[gateway] bench ${runId} resume failed:`, e))
    return json(state)
}

async function proxy(req: Request, path: string): Promise<Response> {
    const url = new URL(req.url)
    try {
        const r = await fetch(`${API}${path}${url.search}`, {
            method: req.method,
            headers: { "content-type": req.headers.get("content-type") ?? "application/json" },
            body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer()
        })
        return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") ?? "application/json", ...cors } })
    } catch (e) {
        return json({ error: `api unavailable at ${API}: ${e instanceof Error ? e.message : String(e)}` }, 503)
    }
}

function serveStatic(pathname: string): Response {
    if (!existsSync(WEB_DIST)) return json({ error: "web/dist not built; run `make web` for the dev server" }, 404)
    const rel = pathname === "/" ? "index.html" : pathname.slice(1)
    const file = normalize(join(WEB_DIST, rel))
    if (!file.startsWith(WEB_DIST)) return json({ error: "bad path" }, 400)
    return new Response(Bun.file(existsSync(file) ? file : join(WEB_DIST, "index.html")))
}

const server = Bun.serve({
    port: PORT,
    async fetch(req) {
        const { pathname } = new URL(req.url)
        if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors })
        if (pathname === "/health") {
            const [api, act] = await Promise.all([reachable(`${API}/health`), reachable(CONTROL)])
            return json({ ok: true, api, actors: act })
        }
        if (pathname.startsWith("/api/")) return proxy(req, pathname.slice(4))
        const sock = pathname.match(/^\/socket\/([A-Za-z]+)\/([^/]+)$/)
        if (sock && req.method === "POST") return grant(sock[1]!, decodeURIComponent(sock[2]!))
        const bench = pathname.match(/^\/bench\/([^/]+)\/begin$/)
        if (bench && req.method === "POST") return beginBench(decodeURIComponent(bench[1]!), (await req.json().catch(() => ({}))) as BeginBody)
        return serveStatic(pathname)
    }
})

console.log(`[gateway] http://127.0.0.1:${server.port}  api=${API}  actors=${CONTROL}`)
