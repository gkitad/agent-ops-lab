"use client";

import styles from "@/app/ui.module.css";
import { FAULTS, MODEL_NAMES, type Bench, type BenchRow } from "@/lib/api";

function clock(s: number | null) {
  if (s == null) return "-";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function avg(xs: number[]) {
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
}

function Cell({ row, onOpen }: { row?: BenchRow; onOpen: (id: string) => void }) {
  if (!row) return <td className={styles.benchPending}>…</td>;
  const ok = row.valid !== false && row.resolved;
  const bait = row.trap?.followed;
  return (
    <td>
      <button className={styles.benchCell} onClick={() => onOpen(row.incident)} title="Open this incident">
        <span className={ok ? styles.benchOk : row.valid === false ? styles.benchVoid : styles.benchBad}>
          {row.valid === false ? "Didn't count" : ok ? "Resolved" : "Not resolved"}
        </span>
        <span className={styles.benchMeta}>
          {row.steps} steps · {clock(row.seconds)}
        </span>
        <span className={bait ? styles.benchBait : styles.benchSafe}>{bait ? "Took the bait" : row.trap?.flagged ? "Ignored, flagged" : "Ignored"}</span>
      </button>
    </td>
  );
}

export default function BenchTable({ bench, onOpen }: { bench: Bench; onOpen: (id: string) => void }) {
  const counted = (m: string) => bench.results.filter((r) => r.model === m && r.valid !== false);
  return (
    <div className={styles.benchWrap}>
      <table className={styles.bench}>
        <thead>
          <tr>
            <th scope="col">Fault</th>
            {bench.models.map((m) => (
              <th scope="col" key={m}>
                {MODEL_NAMES[m] ?? m}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {bench.faults.map((f) => (
            <tr key={f}>
              <th scope="row">{FAULTS[f]?.title ?? f}</th>
              {bench.models.map((m) => (
                <Cell key={m} row={[...bench.results].reverse().find((r) => r.model === m && r.fault === f)} onOpen={onOpen} />
              ))}
            </tr>
          ))}
        </tbody>
        <tfoot>
          {[
            ["Resolved", (m: string) => `${counted(m).filter((r) => r.resolved).length} of ${counted(m).length}`],
            ["Took the bait", (m: string) => `${counted(m).filter((r) => r.trap?.followed).length} of ${counted(m).length}`],
            ["Flagged the planted instruction", (m: string) => `${counted(m).filter((r) => r.trap?.flagged).length} of ${counted(m).length}`],
            ["Average steps", (m: string) => (counted(m).length ? avg(counted(m).map((r) => r.steps)).toFixed(1) : "-")],
            ["Average time", (m: string) => (counted(m).length ? clock(Math.round(avg(counted(m).map((r) => r.seconds ?? 0)))) : "-")],
            ["Average tokens", (m: string) => (counted(m).length ? `${(avg(counted(m).map((r) => r.tokens)) / 1000).toFixed(0)}k` : "-")],
          ].map(([label, fn]) => (
            <tr key={label as string}>
              <th scope="row">{label as string}</th>
              {bench.models.map((m) => (
                <td key={m}>{(fn as (m: string) => string)(m)}</td>
              ))}
            </tr>
          ))}
        </tfoot>
      </table>
    </div>
  );
}
