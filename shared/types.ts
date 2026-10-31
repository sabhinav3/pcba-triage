// Frozen contracts (T+0:20). Mirror of core/triage/schemas.py and CONTRACTS.md. Lead-only edits.

export type Role = "power_rail" | "ground" | "clock" | "qspi" | "usb" | "gpio" | "reset" | "other";
export type FaultClass = "RAIL_SHORT" | "RAIL_OPEN" | "WRONG_VALUE" | "CLOCK_FAIL" | "PIN_BRIDGE" | "SIGNAL_OPEN";
export type MeasKind = "v" | "r_gnd" | "r_pair" | "freq" | "i_supply" | "logic" | "uart" | "usb_enum";
export type Method = "none" | "rules" | "agent";
export type Stage = "S0" | "S1" | "S2" | "S3" | "S4" | "S5" | "S6" | "S7" | "S8" | "PROBE";

export const FAULT_CLASSES: FaultClass[] = ["RAIL_SHORT", "RAIL_OPEN", "WRONG_VALUE", "CLOCK_FAIL", "PIN_BRIDGE", "SIGNAL_OPEN"];
export const STAGES: { id: Stage; label: string; simulated: boolean }[] = [
  { id: "S0", label: "Visual/AOI + BOM", simulated: false },
  { id: "S1", label: "Unpowered R-to-GND", simulated: true },
  { id: "S2", label: "Current-limited power-up", simulated: true },
  { id: "S3", label: "Rail voltages", simulated: true },
  { id: "S4", label: "12 MHz clock", simulated: true },
  { id: "S5", label: "Reset / boot path", simulated: true },
  { id: "S6", label: "SWD / flash ID", simulated: false },
  { id: "S7", label: "Firmware smoke", simulated: true },
  { id: "S8", label: "USB enumeration", simulated: true },
];

export interface Pin { pin: string; name: string; net: string; x?: number | null; y?: number | null }
export interface Component { ref: string; value: string; footprint: string; x: number; y: number; rot: number; dnp: boolean; pins: Pin[] }
export interface NetNode { ref: string; pin: string }
export interface Net { name: string; role: Role; nodes: NetNode[] }
export interface PowerEdge { ref: string; in_net: string; out_net: string; nominal_v: number }
export interface Nominal { v?: number | null; r_gnd?: number | null; r_gnd_min?: number | null; freq_hz?: number | null }
export interface TestPoint { tp: string; net: string; ref: string; pin: string; kinds: MeasKind[] }
export interface Outline { w: number; h: number }
export interface Board {
  board_id: string;
  components: Component[];
  nets: Net[];
  power_tree: PowerEdge[];
  nominal: Record<string, Nominal>;
  testpoints: TestPoint[];
  outline: Outline;
}

export interface Measurement { tp: string; tp2?: string | null; kind: MeasKind; value: number | string; unit: string; stage: Stage }
export interface FaultTarget { nets: string[]; refs: string[]; pins: string[] }
/** Hidden during triage. Only appears in post-run reports. */
export interface FaultCase { case_id: string; cls: FaultClass; target: FaultTarget; params: Record<string, number | string>; seed: number }
export interface Observation { board_public: Board; measurements: Measurement[]; probe_budget: number }
export interface ProbeRequest { tp: string; kind: MeasKind; tp2?: string | null }
export interface Hypothesis {
  rank: number; cls: FaultClass; nets: string[]; refs: string[]; confidence: number; evidence: string[]; next_probe?: ProbeRequest | null;
}
export interface TriageResult {
  case_id: string; method: Method; hypotheses: Hypothesis[]; probes: Measurement[]; probes_used: number;
  bench_seconds: number; wall_ms: number; cost_usd: number | null; notes: string[];
}

// ---- Benchmark output (results/<run_id>/results.json) ----
export interface CaseScore {
  case_id: string; method: Method; truth_cls: FaultClass; truth_nets: string[]; truth_refs: string[];
  pred_cls: FaultClass | null; pred_nets: string[]; pred_refs: string[]; top1_net: boolean; top3_net: boolean; top1_cls: boolean; top1_ref: boolean;
  probes_used: number; bench_seconds: number; wall_ms: number; cost_usd: number | null;
  error?: string | null;   // "error: <Type>: <msg>" when the method crashed on this case (scored as a miss)
}
export interface MethodSummary {
  method: Method; n: number; skipped: boolean; skip_reason?: string | null;
  top1: number; top1_k: number; top1_ci: [number, number]; top3: number; top3_k: number; top3_ci: [number, number];
  cls_acc: number; ref_acc: number; per_class: Record<string, { n: number; top1: number; top3: number; cls: number }>;
  confusion: Record<string, Record<string, number>>;
  mean_probes: number; mean_bench_s: number; mean_wall_ms: number; total_cost_usd: number | null;
  errors?: number;         // cases where the method crashed; counted as misses in every metric
}
export interface BenchResults {
  run_id: string; board_id: string; created_at: string; seed: number; probe_budget: number; prompt_version: string;
  methods: MethodSummary[]; cases: CaseScore[]; results: TriageResult[]; faults: FaultCase[];
  comparisons: { a: Method; b: Method; metric: string; n: number; only_a: number; only_b: number; p_exact: number }[];
  /** standard panel per case (identical input every method saw) */
  panels: Record<string, Measurement[]>;
  model?: string | null;
}

// ---- Actor state (BoardSession) broadcast to the dashboard ----
export type SessionStatus = "idle" | "panel" | "probing" | "triaged" | "error";
export interface SessionState {
  board_id: string; case_id: string; method: Method; stage: Stage | null; status: SessionStatus;
  measurements: Measurement[]; probes: Measurement[]; hypotheses: Hypothesis[]; error?: string | null;
}
/** Socket messages from the actors. ("state"/"state_update" are reserved by durable-actors.) */
export type SessionOutgoing = { type: "session"; state: SessionState };
export type BenchOutgoing = { type: "bench"; state: BenchRunState };
export type SessionMessage =
  | { type: "start" }
  | { type: "probe"; probe: ProbeRequest }
  | { type: "triage" };

export type BenchStatus = "idle" | "running" | "done" | "error";
export interface BenchRunState {
  run_id: string; status: BenchStatus; queue: { case_id: string; method: Method }[]; cursor: number;
  results: TriageResult[]; error?: string | null;
}
