"use client";

import { useEffect, useRef, useState } from "react";
import styles from "@/app/ui.module.css";
import type { LabEvent } from "@/lib/api";

type Approval = Extract<LabEvent, { type: "approval" }>;
type Call = Extract<LabEvent, { type: "tool_call" }>;
type Result = Extract<LabEvent, { type: "tool_result" }>;

export type Item =
  | { kind: "note"; key: string; text: string }
  | { kind: "meta"; key: string; text: string; tone?: "fail" | "ok" }
  | { kind: "call"; key: string; call: Call; result?: Result; approval?: Approval }
  | { kind: "error"; key: string; text: string };

export const CHANGE_TOOLS = new Set([
  "restart_service",
  "delete_file",
  "update_dns_record",
  "renew_certificate",
  "reload_load_balancer",
  "terminate_db_sessions",
]);

/** Fold the raw event stream into timeline items: each tool call carries its result and approval. */
export function buildItems(events: LabEvent[]): Item[] {
  const items: Item[] = [];
  const calls = new Map<string, Extract<Item, { kind: "call" }>>();
  const byApproval = new Map<string, Extract<Item, { kind: "call" }>>();
  for (const e of events) {
    if (e.type === "note") items.push({ kind: "note", key: `n${e.seq}`, text: e.text });
    else if (e.type === "status" && e.phase !== "restoring") items.push({ kind: "meta", key: `s${e.seq}`, text: e.text, tone: e.verdict && e.verdict !== "PASSED" ? "fail" : undefined });
    else if (e.type === "tool_call") {
      const item = { kind: "call" as const, key: e.id, call: e };
      calls.set(e.id, item);
      items.push(item);
    } else if (e.type === "tool_result") {
      const item = calls.get(e.id);
      if (item) item.result = e;
    } else if (e.type === "approval") {
      let item = byApproval.get(e.approvalId);
      if (!item) {
        item = [...calls.values()].reverse().find((c) => c.call.tool === e.tool && !c.result && !c.approval);
        if (item) byApproval.set(e.approvalId, item);
      }
      if (item) item.approval = e;
    } else if (e.type === "error") items.push({ kind: "error", key: `e${e.seq}`, text: e.text });
    else if (e.type === "report") items.push({ kind: "meta", key: `r${e.seq}`, text: "Report filed. The agent closed the incident." });
  }
  return items;
}

const TITLES: Record<string, string> = {
  check_service: "Ran the user check",
  list_hosts: "Listed the servers",
  recent_changes: "Read the change log",
  read_logs: "Read logs",
  run_command: "Ran",
  restart_service: "Restart",
  delete_file: "Delete file",
  update_dns_record: "Change DNS record",
  renew_certificate: "Renew TLS certificate",
  reload_load_balancer: "Reload load balancer",
  terminate_db_sessions: "End database sessions",
};

function chips(call: Call): string[] {
  const i = call.input as Record<string, string | number | null | undefined>;
  switch (call.tool) {
    case "run_command":
      return [String(i.host)];
    case "read_logs":
      return [String(i.host), ...(i.contains ? [`contains "${i.contains}"`] : []), ...(i.since ? [`last ${i.since}`] : [])];
    case "restart_service":
      return [String(i.host)];
    case "delete_file":
      return [String(i.host), String(i.path)];
    case "update_dns_record":
      return [`${i.name} → ${i.ip}`];
    case "terminate_db_sessions":
      return [i.user ? `user ${i.user}` : "", i.query_contains ? `query has "${i.query_contains}"` : ""].filter(Boolean);
    default:
      return [];
  }
}

function verdictOf(out?: string) {
  return out?.match(/Result: (\w+)/)?.[1];
}

const APPROVAL_TEXT: Record<Approval["status"], string> = {
  pending: "Waiting for approval",
  approved: "Approved",
  executed: "Approved and applied",
  denied: "Denied",
  expired: "No decision in time",
};

function ApprovalCard({
  approval,
  canDecide,
  onDecide,
}: {
  approval: Approval;
  canDecide: boolean;
  onDecide: (id: string, approve: boolean, note: string) => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const live = approval.status === "pending";
  async function decide(approve: boolean) {
    setSending(true);
    try {
      await onDecide(approval.approvalId, approve, note);
    } finally {
      setSending(false);
    }
  }
  const by = approval.decidedBy === "policy:auto" ? "auto-approved" : approval.decidedBy === "you" ? "by you" : approval.decidedBy ? `by ${approval.decidedBy}` : "";
  return (
    <div className={`${styles.approval} ${live ? styles.approvalLive : ""}`} id={live ? "live-approval" : undefined}>
      <p className={styles.approvalHead}>
        {live ? (canDecide ? "The agent wants to make a change" : "Waiting for the person who started this incident to approve") : `${APPROVAL_TEXT[approval.status]} ${by}`}
      </p>
      <p className={styles.tHead}>
        <strong>{approval.summary}</strong>
      </p>
      <p className={styles.tReason}>{approval.reason}</p>
      {approval.note && <p className={styles.risk}>Your note: {approval.note}</p>}
      {live && canDecide && (
        <>
          <input
            className={styles.noteInput}
            placeholder="Optional note for the agent (it reads this if you deny)"
            value={note}
            maxLength={300}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className={styles.actions}>
            <button className={styles.btnPrimary} onClick={() => decide(true)} disabled={sending}>
              Approve change
            </button>
            <button className={styles.btnGhost} onClick={() => decide(false)} disabled={sending}>
              Deny
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function CallItem({ item, canDecide, onDecide }: { item: Extract<Item, { kind: "call" }>; canDecide: boolean; onDecide: (id: string, approve: boolean, note: string) => Promise<void> }) {
  const { call, result, approval } = item;
  const out = result?.output ?? "";
  const blocked = out.startsWith("BLOCKED");
  const denied = out.startsWith("DENIED") || out.startsWith("No decision");
  const isChange = CHANGE_TOOLS.has(call.tool);
  const verdict = call.tool === "check_service" ? verdictOf(out) : undefined;
  const tone = blocked || denied ? "fail" : verdict ? (verdict === "PASSED" ? "ok" : verdict === "DEGRADED" ? "warn" : "fail") : isChange && result ? "ok" : "none";
  const command = call.tool === "run_command" ? String((call.input as { command?: string }).command ?? "") : null;
  return (
    <li className={`${styles.tItem} ${styles[`f_${tone}`]}`}>
      <span className={styles.tDot} aria-hidden="true" />
      <div className={styles.tBody}>
        <p className={styles.tHead}>
          {blocked && <span className={`${styles.changeTag} ${styles.tagBlocked}`}>Blocked by policy</span>}
          {isChange && !blocked && <span className={styles.changeTag}>Change</span>}
          <strong>{TITLES[call.tool] ?? call.tool}</strong>
          {command && <code className={styles.cmd}>{command}</code>}
          {chips(call).map((c) => (
            <code key={c} className={styles.chip}>
              {c}
            </code>
          ))}
          {verdict && <span className={`${styles.verdict} ${styles[`v_${verdict}`]}`}>{verdict}</span>}
        </p>
        {approval && <ApprovalCard approval={approval} canDecide={canDecide} onDecide={onDecide} />}
        {blocked ? (
          <p className={styles.blockedText}>{out.replace(/^BLOCKED by policy: /, "")}</p>
        ) : result ? (
          <details className={styles.output} open={isChange}>
            <summary>{isChange ? "Result" : "Output"}</summary>
            <pre>{out}</pre>
          </details>
        ) : !approval ? (
          <p className={styles.running}>
            <span className={styles.spinner} aria-hidden="true" /> Running
          </p>
        ) : null}
      </div>
    </li>
  );
}

export default function Timeline({
  items,
  working,
  canDecide,
  onDecide,
}: {
  items: Item[];
  working: boolean;
  canDecide: boolean;
  onDecide: (id: string, approve: boolean, note: string) => Promise<void>;
}) {
  const end = useRef<HTMLLIElement>(null);
  const count = items.length;
  const pending = items.some((i) => i.kind === "call" && i.approval?.status === "pending");
  const callRunning = items.some((i) => i.kind === "call" && !i.result);
  useEffect(() => {
    const el = end.current;
    const box = el?.closest("[data-scroll]") as HTMLElement | null;
    if (el && box && box.scrollHeight > box.clientHeight) box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  }, [count, pending]);
  useEffect(() => {
    if (pending && canDecide && window.innerWidth <= 980) document.getElementById("live-approval")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [pending, canDecide]);

  return (
    <ol className={styles.timeline} aria-live="polite">
      {items.map((item) => {
        switch (item.kind) {
          case "note":
            return (
              <li key={item.key} className={styles.tNote}>
                {item.text}
              </li>
            );
          case "meta":
            return (
              <li key={item.key} className={`${styles.tMeta} ${item.tone === "fail" ? styles.tMetaFail : ""}`}>
                {item.text}
              </li>
            );
          case "error":
            return (
              <li key={item.key} className={styles.tError} role="alert">
                {item.text}
              </li>
            );
          case "call":
            return <CallItem key={item.key} item={item} canDecide={canDecide} onDecide={onDecide} />;
        }
      })}
      {working && !pending && !callRunning && (
        <li className={styles.tWorking}>
          <span className={styles.spinner} aria-hidden="true" /> Thinking
        </li>
      )}
      <li ref={end} aria-hidden="true" className={styles.tEnd} />
    </ol>
  );
}
