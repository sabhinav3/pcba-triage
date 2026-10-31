import { Actor, type ActorSocket, Ephemeral, Interleave, Persisted } from "durable-actors"

import type {
    BenchMessage, BenchOutgoing, BenchRunState, BenchStatus, Hypothesis, Measurement, Method, ProbeRequest, QueueItem,
    SessionMessage, SessionOutgoing, SessionState, SessionStatus, Stage, TriageResult
} from "./types.js"

const API = process.env.TRIAGE_API_URL ?? "http://127.0.0.1:8000"
const PANEL_STAGES: Stage[] = ["S1", "S2", "S3", "S4", "S5", "S7", "S8"]

async function api<T>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${API}${path}`, {
        method: body === undefined ? "GET" : "POST",
        headers: { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body)
    })
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`)
    return (await res.json()) as T
}

/** actorId = `${board}-${case}-${method}`; board ids may contain '-' so parse from the right. */
function parseSessionId(id: string): { board_id: string; case_id: string; method: Method } {
    const parts = id.split("-")
    const method = (parts.pop() ?? "rules") as Method
    const case_id = parts.pop() ?? ""
    return { board_id: parts.join("-"), case_id, method }
}

type SessionSocket = ActorSocket<null, SessionOutgoing>

export class BoardSession extends Actor<null, SessionMessage, SessionOutgoing> {
    @Persisted stage: Stage | null = null
    @Persisted status: SessionStatus = "idle"
    @Persisted measurements: Measurement[] = []
    @Persisted probes: Measurement[] = []
    @Persisted hypotheses: Hypothesis[] = []
    @Persisted error: string | null = null

    #snapshot(): SessionState {
        return {
            ...parseSessionId(this.id), stage: this.stage, status: this.status, measurements: this.measurements,
            probes: this.probes, hypotheses: this.hypotheses, error: this.error
        }
    }

    #push(): void {
        this.broadcast({ type: "session", state: this.#snapshot() })
    }

    #fail(e: unknown): void {
        this.status = "error"
        this.error = e instanceof Error ? e.message : String(e)
    }

    async getState(): Promise<SessionState> {
        return this.#snapshot()
    }

    @Interleave
    async start(): Promise<SessionState> {
        const { case_id } = parseSessionId(this.id)
        this.status = "panel"
        this.stage = "S0"
        this.measurements = []
        this.probes = []
        this.hypotheses = []
        this.error = null
        this.#push()
        try {
            const panel = await api<{ measurements: Measurement[] }>(`/session/${case_id}/panel`, {})
            for (const st of PANEL_STAGES) {
                this.stage = st
                this.measurements = [...this.measurements, ...panel.measurements.filter(m => m.stage === st)]
                this.#push()
            }
            this.status = "probing"
        } catch (e) {
            this.#fail(e)
        }
        this.#push()
        return this.#snapshot()
    }

    @Interleave
    async probe(req: ProbeRequest): Promise<SessionState> {
        const { case_id } = parseSessionId(this.id)
        try {
            const r = await api<{ measurement: Measurement }>(`/session/${case_id}/probe`, req)
            this.probes = [...this.probes, r.measurement]
            this.stage = "PROBE"
        } catch (e) {
            this.error = e instanceof Error ? e.message : String(e)
        }
        this.#push()
        return this.#snapshot()
    }

    @Interleave
    async triage(): Promise<SessionState> {
        const { case_id, method } = parseSessionId(this.id)
        try {
            const r = await api<TriageResult>(`/triage/${case_id}`, { method })
            this.hypotheses = r.hypotheses
            this.probes = r.probes
            this.status = "triaged"
            this.error = r.notes.find(n => n.startsWith("error")) ?? null
        } catch (e) {
            this.#fail(e)
        }
        this.#push()
        return this.#snapshot()
    }

    async onConnect(socket: SessionSocket): Promise<void> {
        socket.send({ type: "session", state: this.#snapshot() })
    }

    async onMessage(_socket: SessionSocket, msg: SessionMessage): Promise<void> {
        if (msg.type === "start") await this.start()
        else if (msg.type === "probe") await this.probe(msg.probe)
        else if (msg.type === "triage") await this.triage()
    }
}

type BenchSocket = ActorSocket<null, BenchOutgoing>

export class BenchmarkRun extends Actor<null, BenchMessage, BenchOutgoing> {
    @Persisted status: BenchStatus = "idle"
    @Persisted queue: QueueItem[] = []
    @Persisted cursor = 0
    @Persisted results: TriageResult[] = []
    @Persisted error: string | null = null
    @Ephemeral looping = false

    #snapshot(): BenchRunState {
        return { run_id: this.id, status: this.status, queue: this.queue, cursor: this.cursor, results: this.results, error: this.error }
    }

    #push(): void {
        this.broadcast({ type: "bench", state: this.#snapshot() })
    }

    async getState(): Promise<BenchRunState> {
        return this.#snapshot()
    }

    /** Queue methods x cases. Does not run anything: call resume() (or send {type:"resume"}) to start/continue. */
    async begin(board_id: string, methods: Method[], case_ids: string[] | null): Promise<BenchRunState> {
        const cases = case_ids ?? (await api<{ case_id: string }[]>(`/cases/${board_id}`)).map(c => c.case_id)
        this.queue = methods.flatMap(method => cases.map(case_id => ({ case_id, method })))
        this.cursor = 0
        this.results = []
        this.error = null
        this.status = "running"
        this.#push()
        return this.#snapshot()
    }

    /** Runs cases sequentially from the persisted cursor; the cursor is saved after every case. */
    @Interleave
    async resume(): Promise<BenchRunState> {
        if (this.status !== "running" || this.looping) return this.#snapshot()
        this.looping = true
        try {
            while (this.cursor < this.queue.length && this.status === "running") {
                const item = this.queue[this.cursor]!
                const r = await api<TriageResult>(`/triage/${item.case_id}`, { method: item.method })
                this.results = [...this.results, r]
                this.cursor = this.cursor + 1
                this.#push()
            }
            if (this.cursor >= this.queue.length) this.status = "done"
        } catch (e) {
            this.status = "error"
            this.error = e instanceof Error ? e.message : String(e)
        } finally {
            this.looping = false
        }
        this.#push()
        return this.#snapshot()
    }

    async pause(): Promise<BenchRunState> {
        if (this.status === "running") this.status = "idle"
        this.#push()
        return this.#snapshot()
    }

    async onConnect(socket: BenchSocket): Promise<void> {
        socket.send({ type: "bench", state: this.#snapshot() })
        if (this.status === "running" && !this.looping) void this.resume()
    }

    async onMessage(_socket: BenchSocket, msg: BenchMessage): Promise<void> {
        if (msg.type === "resume") {
            if (this.status === "idle" && this.cursor < this.queue.length) this.status = "running"
            void this.resume()
        } else if (msg.type === "pause") await this.pause()
    }
}
