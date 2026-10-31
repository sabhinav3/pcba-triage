import type {
    BenchResults, BenchRunState, Board, Component, FaultCase, Hypothesis, Measurement, Method, MethodSummary, SessionState, Stage, TriageResult
} from "../../shared/types"
import { FAULT_CLASSES, STAGES } from "../../shared/types"

const BOARD_ID = "rp2040_easyduino"
const GW: string = import.meta.env.VITE_GATEWAY_URL ?? (location.port === "5173" ? `${location.protocol}//${location.hostname}:8787` : location.origin)
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const SVG = "http://www.w3.org/2000/svg"

const st = {
    board: null as Board | null,
    results: null as BenchResults | null,
    live: false,
    caseId: "c01",
    method: "rules" as Method,
    measurements: [] as Measurement[],
    probes: [] as Measurement[],
    hypotheses: [] as Hypothesis[],
    activeStage: null as Stage | null,
    ws: null as WebSocket | null,
    mockProbeIdx: 0,
    timers: [] as number[]
}

// ---------------- data ----------------
async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
    const r = await fetch(url, init)
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
    return (await r.json()) as T
}

async function boot(): Promise<void> {
    try {
        const h = await getJson<{ api: boolean; actors: boolean }>(`${GW}/health`, { signal: AbortSignal.timeout(1500) })
        st.live = h.api && h.actors
    } catch {
        st.live = false
    }
    const mode = $("mode")
    mode.textContent = st.live ? "LIVE" : "MOCK"
    mode.classList.toggle("live", st.live)
    mode.title = st.live ? `gateway ${GW}` : "gateway not reachable: showing the committed benchmark run"
    st.board = st.live ? await getJson<Board>(`${GW}/api/board/${BOARD_ID}`) : await getJson<Board>("/mock/board.json")
    try {
        st.results = st.live ? await getJson<BenchResults>(`${GW}/api/runs/latest`) : await getJson<BenchResults>("/mock/results.json")
    } catch {
        st.results = await getJson<BenchResults>("/mock/results.json").catch(() => null)
    }
    $("board-name").textContent = `${st.board.board_id} · ${st.board.components.length} parts · ${st.board.nets.length} nets · ${st.board.outline.w}×${st.board.outline.h} mm`
    const ids = st.live ? (await getJson<{ case_id: string }[]>(`${GW}/api/cases/${BOARD_ID}`)).map(c => c.case_id)
        : Object.keys(st.results?.panels ?? {}).sort()
    $("case").innerHTML = ids.map(id => `<option value="${id}">${id}</option>`).join("")
    $<HTMLButtonElement>("btn-bench").disabled = !st.live
    bind()
    await selectCase()
    renderBench()
}

function truth(): FaultCase | undefined {
    return st.results?.faults.find(f => f.case_id === st.caseId)
}

function mockResult(): TriageResult | undefined {
    return st.results?.results.find(r => r.case_id === st.caseId && r.method === st.method)
}

// ---------------- live session ----------------
async function connectSession(): Promise<void> {
    st.ws?.close()
    st.ws = null
    const id = `${BOARD_ID}-${st.caseId}-${st.method}`
    const grant = await getJson<{ websocketUrl: string }>(`${GW}/socket/BoardSession/${id}`, { method: "POST" })
    const ws = new WebSocket(grant.websocketUrl)
    ws.onmessage = ev => {
        const msg = JSON.parse(String(ev.data)) as { type: string; state?: SessionState }
        if (msg.type === "session" && msg.state) applySession(msg.state)
    }
    st.ws = ws
    await new Promise<void>((res, rej) => { ws.onopen = () => res(); ws.onerror = () => rej(new Error("socket error")) })
}

function applySession(s: SessionState): void {
    if (s.case_id !== st.caseId || s.method !== st.method) return
    st.measurements = s.measurements
    st.probes = s.probes
    st.hypotheses = s.hypotheses
    st.activeStage = s.status === "panel" ? s.stage : null
    renderAll()
}

function send(msg: object): void {
    if (st.ws?.readyState === WebSocket.OPEN) st.ws.send(JSON.stringify(msg))
}

// ---------------- actions ----------------
function clearTimers(): void {
    st.timers.forEach(t => clearTimeout(t))
    st.timers = []
}

async function selectCase(): Promise<void> {
    clearTimers()
    st.caseId = $<HTMLSelectElement>("case").value || st.caseId
    st.method = $<HTMLSelectElement>("method").value as Method
    st.measurements = []
    st.probes = []
    st.hypotheses = []
    st.mockProbeIdx = 0
    st.activeStage = null
    if (st.live) {
        try {
            await connectSession()
        } catch (e) {
            console.warn(e)
        }
    }
    renderAll()
}

function runPanel(): void {
    if (st.live) return send({ type: "start" })
    clearTimers()
    const panel = st.results?.panels[st.caseId] ?? []
    st.measurements = []
    st.hypotheses = []
    st.probes = []
    st.mockProbeIdx = 0
    const order = STAGES.map(s => s.id)
    order.forEach((sid, i) => {
        st.timers.push(window.setTimeout(() => {
            st.activeStage = sid
            st.measurements = [...st.measurements, ...panel.filter(m => m.stage === sid)]
            if (i === order.length - 1) st.activeStage = null
            renderAll()
        }, 160 * (i + 1)))
    })
}

function triage(): void {
    if (!st.measurements.length) runPanel()
    if (st.live) return send({ type: "triage" })
    const r = mockResult()
    st.timers.push(window.setTimeout(() => {
        st.hypotheses = r?.hypotheses ?? []
        st.probes = r?.probes ?? []
        st.mockProbeIdx = st.probes.length
        renderAll()
    }, st.measurements.length ? 50 : 1400))
}

function probeNext(): void {
    const next = st.hypotheses[0]?.next_probe
    if (st.live) {
        if (next) send({ type: "probe", probe: next })
        return
    }
    const r = mockResult()
    if (r && st.mockProbeIdx < r.probes.length) {
        st.probes = r.probes.slice(0, ++st.mockProbeIdx)
        renderAll()
    }
}

async function runBench(): Promise<void> {
    if (!st.live) return
    const runId = `ui-${Date.now().toString(36)}`
    $("bench-progress").textContent = "queuing…"
    await getJson(`${GW}/bench/${runId}/begin`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ board_id: BOARD_ID, methods: ["none", "rules"] })
    })
    const grant = await getJson<{ websocketUrl: string }>(`${GW}/socket/BenchmarkRun/${runId}`, { method: "POST" })
    const ws = new WebSocket(grant.websocketUrl)
    ws.onmessage = ev => {
        const msg = JSON.parse(String(ev.data)) as { type: string; state?: BenchRunState }
        if (msg.type !== "bench" || !msg.state) return
        const s = msg.state
        const hits: Record<string, number> = {}
        for (const r of s.results) {
            const f = st.results?.faults.find(x => x.case_id === r.case_id)
            const h = r.hypotheses[0]
            if (f && h && h.nets.some(n => f.target.nets.includes(n))) hits[r.method] = (hits[r.method] ?? 0) + 1
        }
        const by = Object.entries(hits).map(([m, k]) => `${m} top-1 ${k}`).join(" · ")
        $("bench-progress").textContent = `${runId}: ${s.status} ${s.cursor}/${s.queue.length}${by ? " · " + by : ""}`
        if (s.status === "done" || s.status === "error") ws.close()
    }
}

function bind(): void {
    $("case").addEventListener("change", () => void selectCase())
    $("method").addEventListener("change", () => void selectCase())
    $("btn-panel").addEventListener("click", runPanel)
    $("btn-triage").addEventListener("click", triage)
    $("btn-probe").addEventListener("click", probeNext)
    $("btn-bench").addEventListener("click", () => void runBench())
    $("reveal").addEventListener("change", renderAll)
}

// ---------------- formatting / stage evaluation ----------------
function fmt(m: Measurement): string {
    const v = m.value
    if (typeof v === "string") return v === "" ? "(silent)" : v
    switch (m.kind) {
        case "v": return `${v.toFixed(3)} V`
        case "i_supply": return `${v.toFixed(1)} mA`
        case "freq": return v === 0 ? "0 Hz" : `${(v / 1e6).toFixed(6)} MHz`
        case "r_gnd": case "r_pair":
            if (v >= 1e9) return "OL"
            return v >= 1e6 ? `${(v / 1e6).toFixed(2)} MΩ` : v >= 1e3 ? `${(v / 1e3).toFixed(2)} kΩ` : `${v.toPrecision(3)} Ω`
        default: return String(v)
    }
}

function netOf(tp: string): string {
    return st.board?.testpoints.find(t => t.tp === tp)?.net ?? tp
}

function measBad(m: Measurement): boolean {
    const nom = st.board?.nominal[netOf(m.tp)]
    const v = m.value
    switch (m.kind) {
        case "r_gnd": return typeof v === "number" && !!nom?.r_gnd_min && v < nom.r_gnd_min
        case "v": return typeof v === "number" && !!nom?.v && Math.abs(v / nom.v - 1) > 0.1
        case "freq": return typeof v === "number" && !!nom?.freq_hz && Math.abs(v / nom.freq_hz - 1) > 50e-6
        case "i_supply": return typeof v === "number" && (v > 140 || v < 5)
        case "logic": return v !== "toggling"
        case "uart": return v === ""
        case "usb_enum": return v !== "app"
        default: return false
    }
}

function stageStatus(id: Stage): "pass" | "fail" | "pending" | "info" {
    if (id === "S0" || id === "S6") return "info"
    const ms = st.measurements.filter(m => m.stage === id)
    if (!ms.length) return "pending"
    if (id === "S5") return ms.some(m => (m.kind === "v" && typeof m.value === "number" && m.value < 2.5) || (m.kind === "logic" && m.value !== "toggling")) ? "fail" : "pass"
    return ms.some(measBad) ? "fail" : "pass"
}

// ---------------- render ----------------
function renderAll(): void {
    renderStages()
    renderBoard()
    renderHyps()
    renderProbes()
}

function renderStages(): void {
    $("stages").innerHTML = STAGES.map(s => {
        const status = stageStatus(s.id)
        const ms = st.measurements.filter(m => m.stage === s.id)
        const label = status === "info" ? "n/a" : status
        return `<li class="stage ${st.activeStage === s.id ? "active" : ""}">
      <div class="stage-head"><span><span class="stage-id">${s.id}</span><span class="stage-name">${s.label}</span></span>
      <span class="badge ${status}">${label}</span></div>
      ${ms.length ? `<ul class="meas">${ms.map(m => `<li><span>${m.tp.replace(/^TP_/, "")} ${m.kind}</span><span class="${measBad(m) ? "bad" : ""}">${fmt(m)}</span></li>`).join("")}</ul>` : ""}
    </li>`
    }).join("")
}

function heatFor(c: Component): number {
    if (!st.hypotheses.length) return 0
    const top = Math.max(...st.hypotheses.map(h => h.confidence), 1e-6)
    const nets = new Set(c.pins.map(p => p.net))
    let best = 0
    for (const h of st.hypotheses) {
        const w = h.refs.includes(c.ref) ? 1 : h.nets.some(n => nets.has(n) && n !== "GND") ? (c.pins.length > 8 ? 0.35 : 0.7) : 0
        best = Math.max(best, (w * h.confidence) / top)
    }
    return best
}

function heatColor(x: number): string {
    const lo = [43, 49, 56], hi = [255, 90, 60], mid = [245, 176, 65]
    const [a, b, t] = x < 0.5 ? [lo, mid, x / 0.5] : [mid, hi, (x - 0.5) / 0.5]
    return `rgb(${a.map((v, i) => Math.round(v + (b[i]! - v) * t)).join(",")})`
}

function el(tag: string, attrs: Record<string, string | number>, parent?: Element): SVGElement {
    const e = document.createElementNS(SVG, tag)
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v))
    parent?.appendChild(e)
    return e
}

function renderBoard(): void {
    const b = st.board
    const svg = $("board") as unknown as SVGSVGElement
    if (!b) return
    svg.innerHTML = ""
    const { w, h } = b.outline
    const rot = h > w
    const T = (x: number, y: number): [number, number] => (rot ? [y, w - x] : [x, y])
    const W = rot ? h : w, H = rot ? w : h, M = 1.2
    svg.setAttribute("viewBox", `${-M} ${-M} ${W + 2 * M} ${H + 2 * M}`)
    el("rect", { x: 0, y: 0, width: W, height: H, rx: 1, fill: "var(--mask)", stroke: "var(--mask-edge)", "stroke-width": 0.25 }, svg)
    const f = truth()
    const reveal = $<HTMLInputElement>("reveal").checked && f
    const hover = $("hover")
    for (const c of b.components) {
        const pads = c.pins.filter(p => p.x != null && p.y != null).map(p => T(p.x!, p.y!))
        const g = el("g", {}, svg)
        const heat = heatFor(c)
        let cx: number, cy: number, rw: number, rh: number
        if (pads.length) {
            const xs = pads.map(p => p[0]), ys = pads.map(p => p[1])
            const pad = c.pins.length > 8 ? 0.6 : 0.35
            cx = Math.min(...xs) - pad
            cy = Math.min(...ys) - pad
            rw = Math.max(...xs) - Math.min(...xs) + 2 * pad
            rh = Math.max(...ys) - Math.min(...ys) + 2 * pad
            el("rect", { x: cx, y: cy, width: rw, height: rh, rx: 0.2, class: "part", fill: heatColor(heat) }, g)
        } else {
            const [x, y] = T(c.x, c.y)
            cx = x - 1.1; cy = y - 1.1; rw = rh = 2.2
            el("circle", { cx: x, cy: y, r: 1.1, fill: "#0a0d10", stroke: "var(--gold)", "stroke-width": 0.25 }, g)
        }
        if (c.pins.length && (rw > 2.2 || rh > 2.2 || heat > 0.3)) {
            const t = el("text", { x: cx + rw / 2, y: cy + rh / 2, class: "ref-label", "text-anchor": "middle", "dominant-baseline": "central" }, g)
            t.textContent = c.ref
        }
        if (reveal && f.target.refs.includes(c.ref)) el("rect", { x: cx - 0.4, y: cy - 0.4, width: rw + 0.8, height: rh + 0.8, rx: 0.4, class: "truth-ring" }, svg)
        g.addEventListener("mouseenter", () => {
            const nets = [...new Set(c.pins.map(p => p.net))].filter(n => !n.startsWith("unconnected")).slice(0, 6)
            hover.textContent = `${c.ref} ${c.value} · heat ${(heat * 100).toFixed(0)}% · ${nets.join(", ")}`
        })
    }
    const measured = new Set(st.measurements.map(m => m.tp))
    const posOf = (tp: string): [number, number] | null => {
        const t = b.testpoints.find(x => x.tp === tp)
        const comp = t && b.components.find(c => c.ref === t.ref)
        const pin = comp?.pins.find(p => p.pin === t!.pin)
        return pin?.x != null && pin.y != null ? T(pin.x, pin.y) : comp ? T(comp.x, comp.y) : null
    }
    for (const t of b.testpoints) {
        if (!t.tp.startsWith("TP_")) continue
        const p = posOf(t.tp)
        if (p) el("circle", { cx: p[0], cy: p[1], r: measured.has(t.tp) ? 0.38 : 0.25, class: `tp-dot ${measured.has(t.tp) ? "measured" : ""}` }, svg)
    }
    st.probes.forEach((m, i) => {
        const p = posOf(m.tp)
        if (!p) return
        const g = el("g", { class: "probe-mark" }, svg)
        el("circle", { cx: p[0], cy: p[1] - 1.3, r: 0.75 }, g)
        el("text", { x: p[0], y: p[1] - 1.3 }, g).textContent = String(i + 1)
    })
}

function renderHyps(): void {
    const f = $<HTMLInputElement>("reveal").checked ? truth() : undefined
    const list = $("hyps")
    if (!st.hypotheses.length) {
        list.innerHTML = `<li class="empty">${st.measurements.length ? "Panel taken. Run triage to rank faults." : "Run the panel, then triage."}</li>`
        return
    }
    list.innerHTML = st.hypotheses.map(h => {
        const hit = f && h.nets.some(n => f.target.nets.includes(n))
        const np = h.next_probe ? `next: ${h.next_probe.kind} @ ${h.next_probe.tp}${h.next_probe.tp2 ? " ↔ " + h.next_probe.tp2 : ""}` : ""
        return `<li class="hyp ${hit ? "hit" : ""}">
      <div class="hyp-head"><span class="rank">#${h.rank}</span><span class="cls">${h.cls}</span><span class="nets">${h.nets.join(", ")}</span>
      ${h.refs.length ? `<span class="muted">${h.refs.join(", ")}</span>` : ""}</div>
      <div class="conf"><i style="width:${Math.round(h.confidence * 100)}%"></i></div>
      <ul class="ev">${h.evidence.slice(0, 3).map(e => `<li>${escapeHtml(e)}</li>`).join("")}</ul>
      ${np ? `<div class="next">${np}</div>` : ""}</li>`
    }).join("") + (f ? `<li class="empty">Injected: <b>${f.cls}</b> on ${f.target.nets.join(", ")} (${f.target.refs.join(", ")})</li>` : "")
}

function renderProbes(): void {
    $("probes").innerHTML = st.probes.length
        ? st.probes.map((m, i) => `<li><span class="n">${i + 1}</span><span>${m.kind} ${m.tp.replace(/^TP_/, "")}${m.tp2 ? "↔" + m.tp2.replace(/^TP_/, "") : ""}</span><span>= ${fmt(m)}</span></li>`).join("")
        : `<li class="empty">No extra probes yet.</li>`
}

function escapeHtml(s: string): string {
    return s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!)
}

function renderBench(): void {
    const r = st.results
    if (!r) {
        $("bench-table").innerHTML = "<tr><td>No benchmark results yet. Run <code>make bench</code>.</td></tr>"
        return
    }
    $("run-id").textContent = `· ${r.run_id} · K=${r.probe_budget} · seed ${r.seed}`
    const pct = (x: number) => `${Math.round(x * 100)}%`
    const ci = (c: [number, number]) => `<span class="ci">[${pct(c[0])}–${pct(c[1])}]</span>`
    const rows = r.methods.map((s: MethodSummary) => s.skipped || !s.n
        ? `<tr><td>${s.method}</td><td colspan="7" class="ci" style="text-align:left">skipped: ${escapeHtml(s.skip_reason ?? "")}</td></tr>`
        : `<tr><td>${s.method}</td><td>${s.top1_k}/${s.n} ${pct(s.top1)} ${ci(s.top1_ci)}</td><td>${s.top3_k}/${s.n} ${pct(s.top3)} ${ci(s.top3_ci)}</td>
        <td>${pct(s.cls_acc)}</td><td>${pct(s.ref_acc)}</td><td>${s.mean_probes.toFixed(2)}</td><td>${Math.round(s.mean_bench_s)}</td>
        <td>${s.method === "agent" ? (s.total_cost_usd == null ? "n/a" : "$" + s.total_cost_usd.toFixed(3)) : "$0"}</td></tr>`).join("")
    $("bench-table").innerHTML = `<tr><th>method</th><th>top-1 net (95% CI)</th><th>top-3 net</th><th>class</th><th>refdes</th><th>probes</th><th>bench s</th><th>cost</th></tr>${rows}`
    const ran = r.methods.filter(s => !s.skipped && s.n)
    $("class-bars").innerHTML = FAULT_CLASSES.map(c => `<div class="bar-row"><span class="bar-label">${c}</span><div class="bar-stack">${
        ran.map(s => { const pc = s.per_class[c]!; return `<div class="bar m-${s.method}" title="${s.method}: ${pc.top1}/${pc.n} top-1"><i style="width:${pc.n ? (100 * pc.top1) / pc.n : 0}%"></i></div>` }).join("")
    }</div></div>`).join("") + `<div class="bar-legend">${ran.map(s => `<span><i class="m-${s.method}" style="background:${s.method === "none" ? "#6e7781" : s.method === "rules" ? "var(--info)" : "var(--accent)"}"></i>${s.method} top-1 per class</span>`).join("")}</div>`
    const a = r.methods.find(s => s.method === "agent"), rl = r.methods.find(s => s.method === "rules")
    $("verdict").innerHTML = !a || a.skipped || !a.n
        ? `Agent not evaluated in this run${a?.skip_reason ? ` (${escapeHtml(a.skip_reason)})` : ""}. n=${rl?.n ?? 0} cases, so 95% intervals are wide.`
        : a.top1 > (rl?.top1 ?? 0) ? `Agent ahead of rules on top-1 (${pct(a.top1)} vs ${pct(rl!.top1)}); see the paired test in report.md before calling it a win.`
            : `<b>Agent does not beat rules</b> on top-1 (${pct(a.top1)} vs ${pct(rl?.top1 ?? 0)}).`
}

void boot()
