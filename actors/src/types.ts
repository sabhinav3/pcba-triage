// Mirror of the shared/types.ts subset the actors use. shared/types.ts is the source of truth (lead-only).
// Kept local because the durable-actors contract generator reads types from this project only.
export type Method = "none" | "rules" | "agent"
export type FaultClass = "RAIL_SHORT" | "RAIL_OPEN" | "WRONG_VALUE" | "CLOCK_FAIL" | "PIN_BRIDGE" | "SIGNAL_OPEN"
export type MeasKind = "v" | "r_gnd" | "r_pair" | "freq" | "i_supply" | "logic" | "uart" | "usb_enum"
export type Stage = "S0" | "S1" | "S2" | "S3" | "S4" | "S5" | "S6" | "S7" | "S8" | "PROBE"
export interface Measurement { tp: string; tp2?: string | null; kind: MeasKind; value: number | string; unit: string; stage: Stage }
export interface ProbeRequest { tp: string; kind: MeasKind; tp2?: string | null }
export interface Hypothesis {
    rank: number; cls: FaultClass; nets: string[]; refs: string[]; confidence: number; evidence: string[]; next_probe?: ProbeRequest | null
}
export interface TriageResult {
    case_id: string; method: Method; hypotheses: Hypothesis[]; probes: Measurement[]; probes_used: number
    bench_seconds: number; wall_ms: number; cost_usd: number | null; notes: string[]
}
export type SessionStatus = "idle" | "panel" | "probing" | "triaged" | "error"
export interface SessionState {
    board_id: string; case_id: string; method: Method; stage: Stage | null; status: SessionStatus
    measurements: Measurement[]; probes: Measurement[]; hypotheses: Hypothesis[]; error?: string | null
}
export type SessionMessage = { type: "start" } | { type: "probe"; probe: ProbeRequest } | { type: "triage" }
export type SessionOutgoing = { type: "session"; state: SessionState }  // "state" is reserved by durable-actors
export type BenchStatus = "idle" | "running" | "done" | "error"
export interface QueueItem { case_id: string; method: Method }
export interface BenchRunState {
    run_id: string; status: BenchStatus; queue: QueueItem[]; cursor: number; results: TriageResult[]; error?: string | null
}
export type BenchMessage = { type: "resume" } | { type: "pause" }
export type BenchOutgoing = { type: "bench"; state: BenchRunState }
