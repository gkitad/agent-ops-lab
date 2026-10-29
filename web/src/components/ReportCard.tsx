"use client";

import { useState } from "react";
import styles from "@/app/ui.module.css";
import type { Report } from "@/lib/api";

/** Older reports (and non-strict models) sometimes sent a list as one string. */
export function toList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === "string" && v.trim()) return v.split(/\n+/).map((l) => l.replace(/^[-•]\s*/, "").trim()).filter(Boolean);
  return [];
}

function clean(r: Report): Report {
  return { ...r, evidence: toList(r.evidence), follow_ups: toList(r.follow_ups), suspicious_instructions: toList(r.suspicious_instructions) };
}

function markdown(r: Report, id: string, fault: string | null): string {
  return [
    `# Incident ${id}: ${r.summary}`,
    "",
    `**Status:** ${r.resolved ? "Resolved" : "Not resolved"}${fault ? `  \n**Injected fault:** ${fault}` : ""}`,
    "",
    "## Root cause",
    r.root_cause,
    "",
    "## Evidence",
    ...r.evidence.map((e) => `- ${e}`),
    "",
    "## Fix",
    r.fix,
    "",
    "## Verification",
    r.verification,
    "",
    "## Follow-ups",
    ...r.follow_ups.map((f) => `- [ ] ${f}`),
    ...(r.suspicious_instructions?.length ? ["", "## Instructions found in data and ignored", ...r.suspicious_instructions.map((s) => `- ${s}`)] : []),
  ].join("\n");
}

export default function ReportCard({ report: raw, id, fault }: { report: Report; id: string; fault: string | null }) {
  const [copied, setCopied] = useState(false);
  const report = clean(raw);
  async function copy() {
    try {
      await navigator.clipboard.writeText(markdown(report, id, fault));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }
  return (
    <article className={styles.pm} aria-labelledby="report-title">
      <header className={styles.pmHead}>
        <div>
          <p className={styles.pmMeta}>
            <span className={report.resolved ? styles.pmResolved : styles.pmOpen}>{report.resolved ? "Resolved" : "Not resolved"}</span>
            <span className={styles.muted}>Agent&apos;s report for {id}</span>
          </p>
          <h2 id="report-title" className={styles.pmTitle}>
            {report.summary}
          </h2>
        </div>
        <button className={styles.btnGhost} onClick={copy}>
          {copied ? "Copied" : "Copy as Markdown"}
        </button>
      </header>

      <dl className={styles.pmGrid}>
        <div>
          <dt>Root cause</dt>
          <dd>{report.root_cause}</dd>
        </div>
        <div>
          <dt>Fix</dt>
          <dd>{report.fix}</dd>
        </div>
        <div>
          <dt>Verification</dt>
          <dd>{report.verification}</dd>
        </div>
      </dl>

      <div className={styles.pmCols}>
        <section>
          <h3>Evidence</h3>
          <ul className={styles.pmActions}>
            {report.evidence.map((e, i) => (
              <li key={i}>
                <span>{e}</span>
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h3>Follow-ups</h3>
          <ul className={styles.pmActions}>
            {report.follow_ups.map((f, i) => (
              <li key={i}>
                <span>{f}</span>
              </li>
            ))}
          </ul>
          {!!report.suspicious_instructions?.length && (
            <>
              <h3 className={styles.pmWarnTitle}>Instructions found in data and ignored</h3>
              <ul className={styles.pmActions}>
                {report.suspicious_instructions.map((s, i) => (
                  <li key={i}>
                    <span>{s}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>
    </article>
  );
}
