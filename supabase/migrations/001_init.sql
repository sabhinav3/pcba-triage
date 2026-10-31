-- pcba-triage results store. Written by core/triage/store.py with the service-role key (server side only).
create table if not exists runs (
  run_id text primary key,
  board_id text not null,
  created_at timestamptz not null default now(),
  seed int,
  probe_budget int,
  prompt_version text,
  model text,
  summary jsonb not null,          -- MethodSummary[]
  comparisons jsonb not null default '[]'
);
create table if not exists cases (
  run_id text references runs(run_id) on delete cascade,
  case_id text not null,
  cls text not null,
  target jsonb not null,
  params jsonb not null default '{}',
  seed bigint,
  primary key (run_id, case_id)
);
create table if not exists results (
  run_id text references runs(run_id) on delete cascade,
  case_id text not null,
  method text not null check (method in ('none','rules','agent')),
  hypotheses jsonb not null,
  probes_used int not null,
  bench_seconds real,
  wall_ms real,
  cost_usd real,
  notes jsonb not null default '[]',
  score jsonb,
  primary key (run_id, case_id, method)
);
create table if not exists probes (
  run_id text references runs(run_id) on delete cascade,
  case_id text not null,
  method text not null,
  seq int not null,
  measurement jsonb not null,
  primary key (run_id, case_id, method, seq)
);
alter table runs enable row level security;
alter table cases enable row level security;
alter table results enable row level security;
alter table probes enable row level security;
-- no anon policies: the browser never talks to Supabase directly; the service role bypasses RLS.
