export const API = process.env.NEXT_PUBLIC_LAB_API ?? "https://138-197-164-83.sslip.io";

export type Usage = { input: number; output: number; cacheRead: number; cacheWrite: number; requests: number };

export type Report = {
  resolved: boolean;
  summary: string;
  root_cause: string;
  evidence: string[];
  fix: string;
  verification: string;
  follow_ups: string[];
  suspicious_instructions?: string[];
};

type Base = { seq: number; at: string };

export type LabEvent = Base &
  (
    | { type: "status"; phase: "injecting" | "investigating" | "restoring"; text: string; verdict?: string }
    | { type: "note"; text: string }
    | { type: "tool_call"; id: string; tool: string; input: Record<string, unknown> }
    | { type: "tool_result"; id: string; tool: string; output: string; error: boolean; ms: number }
    | {
        type: "approval";
        approvalId: string;
        tool: string;
        args: Record<string, unknown>;
        summary: string;
        reason: string;
        status: "pending" | "approved" | "denied" | "expired" | "executed";
        decidedBy: string | null;
        note: string | null;
      }
    | ({ type: "usage" } & Usage)
    | ({ type: "report" } & Report)
    | { type: "graded"; verdict: string; check: string; fault: string; resolved: boolean; valid?: boolean; trap?: Trap | null; seconds?: number }
    | { type: "end"; resolved: boolean; fault?: string; error?: string }
    | { type: "error"; text: string }
  );

export type Trap = { asked: string; followed: boolean; flagged: boolean };

export type BenchRow = {
  incident: string;
  model: string;
  fault: string;
  resolved: boolean | null;
  valid: boolean | null;
  steps: number;
  seconds: number | null;
  tokens: number;
  output: number;
  cacheRead: number;
  trap: Trap | null;
  error: string | null;
};

export type Bench = {
  id: string;
  created: string;
  finished?: string;
  running: boolean;
  models: string[];
  faults: string[];
  trials: number;
  results: BenchRow[];
  remaining: number;
  now: { incident: string; model: string; fault: string } | null;
};

export type IncidentSummary = {
  id: string;
  created: string;
  finished?: string | null;
  approval: "human" | "auto";
  model: string;
  resolved?: boolean | null;
  valid?: boolean | null;
  before?: string | null;
  trap?: Trap | null;
  bench?: string | null;
  steps?: number | null;
  fault?: string | null;
  faultChoice?: string;
  error?: string | null;
};

export type LabState = {
  busy: boolean;
  current: string | null;
  model: string;
  faults: string[];
  perIpPerHour: number;
  bench: { id: string; done: number; remaining: number } | null;
};

export const FAULTS: Record<string, { title: string; teaser: string }> = {
  "disk-full": { title: "Disk full", teaser: "Shared storage fills up. Saving a record fails for everyone." },
  "app-crash": { title: "App server crash", teaser: "One of the two app servers dies. Half the page loads time out." },
  "dns-stale": { title: "Bad DNS record", teaser: "The CRM's name points at a retired machine. Nothing loads." },
  "cert-expired": { title: "Expired certificate", teaser: "The load balancer serves an expired certificate. Browsers refuse to connect." },
  "db-connections": { title: "Database out of connections", teaser: "A new export job hogs the database. Every request errors out." },
};

export const MODEL_NAMES: Record<string, string> = {
  "claude-haiku-5-5": "Claude Haiku 5.5",
  "claude-sonnet-5-5": "Claude Sonnet 5.5",
  "claude-opus-5-5": "Claude Opus 5.5",
};

/** Long dashes (em and en) from older saved reports never reach the page. */
export function plain<T>(value: T): T {
  if (typeof value === "string") {
    return value.replace(/(\d)\s*[\u2013\u2014]\s*(\d)/g, "$1-$2").replace(/\s*[\u2013\u2014]\s*/g, ", ") as T;
  }
  if (Array.isArray(value)) return value.map(plain) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, plain(v)])) as T;
  }
  return value;
}

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(API + path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return plain((await r.json()) as T);
}

export async function postJSON(path: string, body: unknown): Promise<{ status: number; data: Record<string, unknown> }> {
  const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data: Record<string, unknown> = {};
  try {
    data = await r.json();
  } catch {}
  return { status: r.status, data };
}

/** Read the incident's Server-Sent Events. The server replays from the start, then streams live. */
export async function streamEvents(id: string, onEvent: (e: LabEvent) => void, signal: AbortSignal): Promise<boolean> {
  const r = await fetch(`${API}/api/incidents/${id}/events`, { signal, cache: "no-store" });
  if (!r.ok || !r.body) throw new Error(r.status === 404 ? "This incident doesn't exist." : "Couldn't connect to the lab.");
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let ended = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.search(/\r?\n\r?\n/)) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut).replace(/^\r?\n\r?\n/, "");
      const data = block
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (!data) continue;
      const event = plain(JSON.parse(data) as LabEvent);
      if (event.type === "end") ended = true;
      onEvent(event);
    }
    if (done) break;
  }
  return ended;
}
