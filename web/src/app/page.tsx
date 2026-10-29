"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import styles from "./ui.module.css";
import Timeline, { buildItems, CHANGE_TOOLS } from "@/components/Timeline";
import ReportCard from "@/components/ReportCard";
import {
  FAULTS,
  MODEL_NAMES,
  getJSON,
  postJSON,
  streamEvents,
  type IncidentSummary,
  type LabEvent,
  type LabState,
  type Report,
  type Usage,
} from "@/lib/api";

const PORTFOLIO = "https://portfolio-eosin-delta-24.vercel.app/";
const SOURCE = "https://github.com/gkitad/agent-ops-lab";

const HOSTS = [
  { name: "lb-01", ip: "172.30.0.40", role: "nginx load balancer, TLS" },
  { name: "crm-app-01", ip: "172.30.0.11", role: "CRM API server" },
  { name: "crm-app-02", ip: "172.30.0.12", role: "CRM API server" },
  { name: "db-01", ip: "172.30.0.21", role: "PostgreSQL 16" },
  { name: "dns-01", ip: "172.30.0.53", role: "CoreDNS" },
  { name: "office-pc", ip: "172.30.0.100", role: "A user's workstation" },
];

function readKey(id: string): string | null {
  try {
    return localStorage.getItem(`aol-key-${id}`);
  } catch {
    return null;
  }
}

function saveKey(id: string, key: string) {
  try {
    localStorage.setItem(`aol-key-${id}`, key);
  } catch {}
}

function clock(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function ago(iso?: string | null) {
  if (!iso) return "";
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}

function useNow(running: boolean) {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!running) return;
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const t = setInterval(tick, 1000);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [running]);
  return now;
}

function Landing({ onOpen }: { onOpen: (id: string, key?: string) => void }) {
  const [lab, setLab] = useState<LabState | null>(null);
  const [recent, setRecent] = useState<IncidentSummary[]>([]);
  const [approval, setApproval] = useState<"human" | "auto">("human");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const [l, r] = await Promise.all([getJSON<LabState>("/api/lab"), getJSON<IncidentSummary[]>("/api/incidents")]);
        if (!alive) return;
        setLab(l);
        setRecent(r);
        setOffline(false);
      } catch {
        if (alive) setOffline(true);
      }
    }
    poll();
    const t = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  async function start(fault: string) {
    setStarting(true);
    setError(null);
    try {
      const { status, data } = await postJSON("/api/incidents", { fault, approval });
      if (status === 201) onOpen(String(data.id), String(data.key));
      else if (status === 409 && data.current) setError("Someone else's incident is running right now. You can watch it live below.");
      else setError(String(data.error ?? "The incident couldn't start. Try again in a moment."));
    } catch {
      setError("Couldn't reach the lab server. Try again in a moment.");
    } finally {
      setStarting(false);
    }
  }

  const busy = !!lab?.busy;
  const finished = recent.filter((r) => r.finished && !r.error);
  const solved = finished.filter((r) => r.resolved).length;

  return (
    <main>
      <section className={styles.window}>
        <div className={styles.chrome} aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div className={styles.hero}>
          <h1 className={styles.heroTitle}>
            Real servers. Real outages.
            <br />
            An AI that <em>fixes them.</em>
          </h1>
          <p className={styles.heroText}>
            This page is wired to a live Ubuntu server running a small company&apos;s CRM. Press start and a real fault is
            injected. Claude gets only the users&apos; complaint and works the incident through an MCP server that allows
            read-only diagnostics and only the changes you approve.
          </p>
        </div>
        <ul className={styles.hosts}>
          {HOSTS.map((h) => (
            <li key={h.name}>
              <span className={`${styles.hostDot} ${offline ? styles.hostDotOff : ""}`} aria-hidden="true" />
              <strong>{h.name}</strong>
              <span>{h.role}</span>
              <code>{h.ip}</code>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.picker} aria-labelledby="pick-title">
        <div className={styles.pickerHead}>
          <h2 id="pick-title" className={styles.sectionTitle}>
            Break something
          </h2>
          <p className={`${styles.labState} ${busy ? styles.labBusy : offline ? styles.labOff : styles.labIdle}`} role="status">
            {offline ? "Lab server unreachable" : !lab ? "Connecting to the lab" : busy ? "An incident is running" : "Lab is healthy and idle"}
          </p>
        </div>

        {busy && lab?.current && (
          <button className={styles.watchLive} onClick={() => onOpen(lab.current!)}>
            <span>Someone is working an incident right now.</span>
            <strong>Watch it live ↗</strong>
          </button>
        )}

        <fieldset className={styles.mode}>
          <legend>Who approves the agent&apos;s changes?</legend>
          <label className={approval === "human" ? styles.modeOn : ""}>
            <input type="radio" name="approval" checked={approval === "human"} onChange={() => setApproval("human")} />
            <span>
              <strong>I do</strong> Each change waits for your click
            </span>
          </label>
          <label className={approval === "auto" ? styles.modeOn : ""}>
            <input type="radio" name="approval" checked={approval === "auto"} onChange={() => setApproval("auto")} />
            <span>
              <strong>Policy</strong> Allowed changes go through on their own
            </span>
          </label>
        </fieldset>

        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        <button className={styles.mystery} onClick={() => start("random")} disabled={starting || busy || offline}>
          <span className={styles.mysteryTitle}>Mystery outage</span>
          <span className={styles.mysteryText}>
            One of the five faults below, picked at random and hidden from you until the incident closes.
          </span>
          <span className={styles.mysteryCta}>{starting ? "Starting…" : "Start incident ↗"}</span>
        </button>
        <ul className={styles.faults}>
          {Object.entries(FAULTS).map(([id, f]) => (
            <li key={id}>
              <button className={styles.fault} onClick={() => start(id)} disabled={starting || busy || offline}>
                <span className={styles.faultTitle}>{f.title}</span>
                <span className={styles.faultText}>{f.teaser}</span>
              </button>
            </li>
          ))}
        </ul>
        <p className={styles.fine}>
          Faults are real: a disk really fills, a process is really killed, DNS really changes. One incident runs at a time,
          and the lab resets itself after each one. The agent runs on {MODEL_NAMES[lab?.model ?? ""] ?? "Claude"} and never
          sees which fault was chosen.
        </p>
      </section>

      <section className={styles.how} aria-labelledby="how-title">
        <h2 id="how-title" className={styles.sectionTitle}>
          How it&apos;s built
        </h2>
        <ol className={styles.flow}>
          <li>
            <strong>Your browser</strong>
            <span>This page, on Vercel</span>
          </li>
          <li>
            <strong>Incident service</strong>
            <span>Injects the fault, streams the timeline, relays your approvals</span>
          </li>
          <li>
            <strong>Claude</strong>
            <span>Plans, reads evidence, asks for changes, writes the report</span>
          </li>
          <li>
            <strong>MCP server</strong>
            <span>Command allowlist, approvals, budgets, audit log</span>
          </li>
          <li>
            <strong>6 containers</strong>
            <span>Docker on a DigitalOcean droplet</span>
          </li>
        </ol>
        <ul className={styles.guards}>
          <li>No shell: every command is parsed and checked against an allowlist before it runs.</li>
          <li>Credentials, private keys and environment variables can&apos;t be read, and output is redacted.</li>
          <li>Database queries run as a read-only Postgres role.</li>
          <li>The agent holds a different token from the approver, so it can&apos;t approve its own changes.</li>
          <li>Every call, allowed or blocked, lands in an audit log.</li>
        </ul>
      </section>

      {recent.length > 0 && (
        <section className={styles.recent} aria-labelledby="recent-title">
          <h2 id="recent-title" className={styles.sectionTitle}>
            Recent incidents{" "}
            {finished.length > 0 && (
              <span className={styles.muted}>
                {solved} of {finished.length} resolved
              </span>
            )}
          </h2>
          <ul className={styles.recentList}>
            {recent.slice(0, 12).map((r) => (
              <li key={r.id}>
                <button onClick={() => onOpen(r.id)}>
                  <span className={`${styles.pill} ${!r.finished ? styles.pillLive : r.resolved ? styles.pillOk : styles.pillBad}`}>
                    {!r.finished ? "Live" : r.resolved ? "Resolved" : "Not resolved"}
                  </span>
                  <span className={styles.recentFault}>{r.fault ? FAULTS[r.fault]?.title ?? r.fault : "Mystery outage"}</span>
                  <span className={styles.muted}>
                    {r.steps ?? 0} steps · {r.approval === "human" ? "human approval" : "policy approval"}
                  </span>
                  <span className={styles.recentWhen}>{ago(r.created)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}

function IncidentView({ id, onBack }: { id: string; onBack: () => void }) {
  const [events, setEvents] = useState<LabEvent[]>([]);
  const [summary, setSummary] = useState<IncidentSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [copied, setCopied] = useState(false);
  const key = useMemo(() => readKey(id), [id]);
  const seen = useRef(new Set<number>());

  useEffect(() => {
    const ctrl = new AbortController();
    let stop = false;
    seen.current = new Set(); // the view is keyed by incident id, so state starts fresh
    (async () => {
      for (let attempt = 0; attempt < 30 && !stop; attempt++) {
        try {
          const done = await streamEvents(
            id,
            (e) => {
              const k = e.type === "end" ? -1 : e.seq;
              if (seen.current.has(k)) return;
              seen.current.add(k);
              setEvents((prev) => [...prev, e]);
            },
            ctrl.signal,
          );
          if (done) {
            setEnded(true);
            break;
          }
        } catch (e) {
          if (stop) return;
          if (e instanceof Error && e.message.includes("doesn't exist")) {
            setError(e.message);
            return;
          }
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    })();
    return () => {
      stop = true;
      ctrl.abort();
    };
  }, [id]);

  useEffect(() => {
    getJSON<IncidentSummary>(`/api/incidents/${id}`).then(setSummary).catch(() => {});
  }, [id, ended]);

  const decide = useCallback(
    async (approvalId: string, approve: boolean, note: string) => {
      if (!key) return;
      const { status, data } = await postJSON(`/api/incidents/${id}/approvals/${approvalId}`, { decision: approve ? "approve" : "deny", note, key });
      if (status !== 200) setError(String(data.error ?? "Couldn't send your decision."));
    },
    [id, key],
  );

  const items = useMemo(() => buildItems(events), [events]);
  const graded = events.find((e): e is Extract<LabEvent, { type: "graded" }> => e.type === "graded");
  const report = events.find((e): e is Extract<LabEvent, { type: "report" }> => e.type === "report") as (Report & { type: "report" }) | undefined;
  const usage = [...events].reverse().find((e): e is Extract<LabEvent, { type: "usage" }> => e.type === "usage") as (Usage & { type: "usage" }) | undefined;
  const pending = items.some((i) => i.kind === "call" && i.approval?.status === "pending");
  const injecting = events.length > 0 && !events.some((e) => e.type === "status" && e.phase === "investigating") && !graded;
  const running = events.length > 0 && !graded && !ended;
  const now = useNow(running);
  const start = events[0] ? Date.parse(events[0].at) : 0;
  const finish = graded ? Date.parse(graded.at) : now;

  const calls = items.filter((i) => i.kind === "call");
  const blocked = calls.filter((c) => c.kind === "call" && c.result?.output.startsWith("BLOCKED")).length;
  const changes = calls.filter((c) => c.kind === "call" && CHANGE_TOOLS.has(c.call.tool) && c.result?.output.startsWith("APPROVED")).length;
  const tokens = usage ? usage.input + usage.output + usage.cacheRead + usage.cacheWrite : 0;
  const touched = new Map<string, number>();
  for (const c of calls) {
    if (c.kind !== "call") continue;
    const host = (c.call.input as { host?: string }).host;
    if (host) touched.set(host, (touched.get(host) ?? 0) + 1);
  }

  const status = graded
    ? graded.resolved
      ? "Resolved"
      : "Not resolved"
    : events.length === 0
      ? "Connecting"
      : injecting
        ? "Injecting fault"
        : pending
          ? "Waiting for approval"
          : "Investigating";
  const stateClasses: Record<string, string> = {
    Resolved: styles.st_Resolved,
    "Not resolved": styles.st_Notresolved,
    "Waiting for approval": styles.st_Waitingforapproval,
  };
  const stateClass = stateClasses[status] ?? styles.st_Investigating;
  const fault = graded?.fault ?? summary?.fault ?? null;
  const chosen = summary?.faultChoice && summary.faultChoice !== "random" ? summary.faultChoice : null;
  const before = events.find((e): e is Extract<LabEvent, { type: "status" }> => e.type === "status" && e.phase === "investigating")?.verdict;

  async function share() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {}
  }

  return (
    <main className={styles.incident}>
      <section className={styles.ticket} aria-label="Incident">
        <div className={styles.ticketRow}>
          <span className={styles.ticketId}>INC-{id.slice(0, 6).toUpperCase()}</span>
          <span className={`${styles.state} ${stateClass}`}>{status}</span>
          <span className={styles.ticketMeta}>
            {fault ? FAULTS[fault]?.title ?? fault : chosen ? FAULTS[chosen]?.title : "Mystery outage"}
            {summary && ` · ${MODEL_NAMES[summary.model] ?? summary.model} · ${summary.approval === "human" ? "human approval" : "policy approval"}`}
          </span>
          <span className={styles.timer}>{start ? clock(finish - start) : "0:00"}</span>
        </div>
        <blockquote className={styles.complaint}>
          <p>&ldquo;The CRM isn&apos;t working for anyone in the office. Can someone look at it?&rdquo;</p>
          <footer>
            Ticket from the front desk{before && before !== "PASSED" && <> · user check: <strong className={styles.failText}>{before}</strong></>}
          </footer>
        </blockquote>
      </section>

      {graded && (
        <section className={`${styles.result} ${graded.resolved ? styles.resultOk : styles.resultBad}`} aria-live="polite">
          <div>
            <p className={styles.resultHead}>{graded.resolved ? "Service restored" : "Service not restored"}</p>
            <p className={styles.resultText}>
              The injected fault was <strong>{(FAULTS[graded.fault]?.title ?? graded.fault).toLowerCase()}</strong>. The final user
              check, run by the lab and not the agent, {graded.verdict === "PASSED" ? "passed" : `ended ${graded.verdict}`}.
            </p>
          </div>
          <dl className={styles.stats}>
            <div>
              <dt>Time</dt>
              <dd>{clock(finish - start)}</dd>
            </div>
            <div>
              <dt>Tool calls</dt>
              <dd>{calls.length}</dd>
            </div>
            <div>
              <dt>Changes</dt>
              <dd>{changes}</dd>
            </div>
          </dl>
        </section>
      )}

      <div className={styles.grid}>
        <div className={styles.left}>
          <section className={styles.panel} aria-labelledby="lab-title">
            <h2 id="lab-title" className={styles.panelTitle}>
              Servers the agent touched
            </h2>
            <ul className={styles.hostList}>
              {HOSTS.map((h) => (
                <li key={h.name} className={touched.has(h.name) ? styles.hostTouched : ""}>
                  <span>
                    <strong>{h.name}</strong>
                    <span className={styles.muted}>{h.role}</span>
                  </span>
                  <span className={styles.hostCount}>{touched.get(h.name) ?? 0}</span>
                </li>
              ))}
            </ul>
          </section>
          <section className={styles.panel} aria-labelledby="numbers-title">
            <h2 id="numbers-title" className={styles.panelTitle}>
              By the numbers
            </h2>
            <dl className={styles.numbers}>
              <div>
                <dt>Tool calls</dt>
                <dd>{calls.length}</dd>
              </div>
              <div>
                <dt>Blocked by policy</dt>
                <dd>{blocked}</dd>
              </div>
              <div>
                <dt>Changes made</dt>
                <dd>{changes}</dd>
              </div>
              <div>
                <dt>Tokens</dt>
                <dd>{tokens ? `${(tokens / 1000).toFixed(1)}k` : "—"}</dd>
              </div>
              <div>
                <dt>From cache</dt>
                <dd>{tokens && usage ? `${Math.round((usage.cacheRead / tokens) * 100)}%` : "—"}</dd>
              </div>
            </dl>
          </section>
        </div>

        <section className={`${styles.panel} ${styles.log}`} aria-labelledby="log-title">
          <h2 id="log-title" className={styles.panelTitle}>
            Investigation
          </h2>
          <div className={styles.logScroll} data-scroll>
            <Timeline items={items} working={running && !injecting} canDecide={!!key && running} onDecide={decide} />
          </div>
          {error && (
            <p role="alert" className={styles.error}>
              {error}
            </p>
          )}
        </section>
      </div>

      {report && <ReportCard report={report} id={`INC-${id.slice(0, 6).toUpperCase()}`} fault={fault ? FAULTS[fault]?.title ?? fault : null} />}

      <div className={styles.again}>
        <button className={styles.btnPrimary} onClick={onBack}>
          {graded ? "Break something else" : "Back to the lab"}
        </button>
        <button className={styles.btnGhost} onClick={share}>
          {copied ? "Link copied" : "Copy link to this incident"}
        </button>
      </div>
    </main>
  );
}

export default function Home() {
  const [id, setId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const read = () => setId(new URLSearchParams(window.location.search).get("incident"));
    const first = setTimeout(() => {
      read();
      setReady(true);
    }, 0);
    window.addEventListener("popstate", read);
    return () => {
      clearTimeout(first);
      window.removeEventListener("popstate", read);
    };
  }, []);

  function open(next: string, key?: string) {
    if (key) saveKey(next, key);
    window.history.pushState(null, "", `?incident=${next}`);
    setId(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function back() {
    window.history.pushState(null, "", window.location.pathname);
    setId(null);
  }

  return (
    <div className={styles.page}>
      <header className={styles.top}>
        <button className={styles.brand} onClick={back}>
          <span className={styles.brandName}>Agent Ops Lab</span>
          <span className={styles.brandBy}>by Anthony Jaimez</span>
        </button>
        <nav className={styles.topLinks} aria-label="Links">
          <a href={SOURCE}>Source ↗</a>
          <a href={PORTFOLIO}>Portfolio ↗</a>
        </nav>
      </header>

      {ready && (id ? <IncidentView key={id} id={id} onBack={back} /> : <Landing onOpen={open} />)}

      <footer className={styles.footer}>
        <p>
          A portfolio project by <a href={PORTFOLIO}>Anthony Jaimez</a>. Claude API, MCP, Python, Docker, Caddy, Next.js.
        </p>
      </footer>
    </div>
  );
}
