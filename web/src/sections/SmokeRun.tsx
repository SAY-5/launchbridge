import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useRef, useState } from "react";
import { Reveal } from "../components/Reveal";
import { runBurst, type BurstPhase, type BurstProgress, type BurstSummary } from "../sim/burst";
import { Service } from "../sim/service";
import { Smoke, SMOKE_CHECK_NAMES, checkLabel, summarize, type CheckResult } from "../sim/smoke";
import { fmtMs, wait } from "../util";

type RowState = "pending" | "running" | "PASS" | "FAIL" | "SKIP";

interface Row {
  name: string;
  state: RowState;
  result?: CheckResult;
}

const PHASES: { id: BurstPhase; label: string }[] = [
  { id: "smoke", label: "smoke suite" },
  { id: "first-pass", label: "250 unique" },
  { id: "duplicates", label: "50 duplicates" },
  { id: "rejections", label: "3 bad signatures" },
  { id: "settling", label: "settle" },
  { id: "replay", label: "bulk replay" },
  { id: "resettling", label: "settle again" },
  { id: "done", label: "summary" },
];

const STATUSES = ["pending", "in_progress", "delivered", "failed", "replayed"] as const;

function initialRows(): Row[] {
  return SMOKE_CHECK_NAMES.map((name) => ({ name, state: "pending" }));
}

export function SmokeRun() {
  const reduce = useReducedMotion();
  const [rows, setRows] = useState<Row[]>(initialRows);
  const [running, setRunning] = useState(false);
  const [summary, setSummary] = useState<{ passed: number; failed: number; skipped: number } | null>(null);
  const runs = useRef(0);

  const [burst, setBurst] = useState<BurstProgress | null>(null);
  const [burstDone, setBurstDone] = useState<BurstSummary | null>(null);
  const [bursting, setBursting] = useState(false);

  const runSmoke = async () => {
    setRunning(true);
    setSummary(null);
    setRows(initialRows());
    runs.current += 1;
    const service = new Service({ seed: 0x51 + runs.current });
    const smoke = new Smoke(service, {
      onStart: (_name, i) => {
        setRows((r) => r.map((row, j) => (j === i ? { ...row, state: "running" } : row)));
      },
      onResult: async (result, i) => {
        setRows((r) => r.map((row, j) => (j === i ? { ...row, state: checkLabel(result), result } : row)));
        await wait(reduce ? 0 : 140);
      },
    });
    const results = await smoke.run();
    setSummary(summarize(results));
    setRunning(false);
  };

  const runTheBurst = async () => {
    setBursting(true);
    setBurstDone(null);
    setBurst(null);
    runs.current += 1;
    const done = await runBurst({
      seed: 0xb0 + runs.current,
      onProgress: async (p) => {
        setBurst(p);
        await wait(reduce ? 0 : 30);
      },
    });
    setBurstDone(done);
    setBursting(false);
  };

  const phaseIndex = burst ? PHASES.findIndex((p) => p.id === burst.phase) : -1;
  const totalDeliveries = burst?.stats
    ? STATUSES.reduce((n, s) => n + burst.stats!.deliveries[s], 0)
    : 0;

  return (
    <section className="section" id="smoke" aria-labelledby="smoke-title">
      <div className="wrap">
        <Reveal className="section-head">
          <span className="eyebrow">04 / Smoke run</span>
          <h2 id="smoke-title">Fourteen of fifteen checks, then three hundred events.</h2>
          <p>
            <code>smoke/smoke.py</code> runs against any base URL and is the acceptance check
            for a deployment. It has fifteen checks; fourteen of them are ported here, all but
            secret rotation, and they run against the port on a fresh service. The burst is
            <code>scripts/demo.py</code> with seven of its eight assertions, the eighth being
            the operations overview. Both run on a virtual clock, so the durations below are
            computed here rather than measured.
          </p>
        </Reveal>

        <div className="smoke">
          <Reveal className="glass panel" delay={0.05}>
            <div className="panel-head">
              <h3>Smoke suite</h3>
              <span className="pill pill-slate">14 of 15 ported</span>
              <button type="button" className="btn btn-gold btn-sm" onClick={runSmoke} disabled={running}>
                {running ? "Running" : summary ? "Run again" : "Run the 14 ported checks"}
              </button>
            </div>
            <ol className="checks" aria-label="Smoke checks">
              {rows.map((row, i) => (
                <li key={row.name} className={`check check-${row.state}`}>
                  <span className="check-n mono">{String(i + 1).padStart(2, "0")}</span>
                  <span className={`check-label mono check-label-${row.state}`}>
                    {row.state === "pending" ? "    " : row.state === "running" ? "...." : row.state}
                  </span>
                  <span className="check-text">
                    <span className="check-name">{row.name}</span>
                    <AnimatePresence>
                      {row.result?.detail && (
                        <motion.span
                          className="check-detail mono"
                          initial={reduce ? false : { opacity: 0 }}
                          animate={{ opacity: 1 }}
                        >
                          {row.result.detail}
                        </motion.span>
                      )}
                    </AnimatePresence>
                  </span>
                  <span className="check-time mono">
                    {row.result ? (
                      <>
                        {row.result.wall_ms.toFixed(1)} ms
                        {row.result.virtual_ms > 0 && <span className="dim"> / {fmtMs(row.result.virtual_ms)} virtual</span>}
                      </>
                    ) : null}
                  </span>
                </li>
              ))}
            </ol>
            <div className="smoke-summary mono" aria-live="polite">
              {summary
                ? `smoke: ${summary.passed} passed, ${summary.failed} failed, ${summary.skipped} skipped`
                : running
                  ? "smoke: running"
                  : "smoke: idle"}
            </div>
          </Reveal>

          <Reveal className="glass panel" delay={0.12}>
            <div className="panel-head">
              <h3>Burst mode</h3>
              <span className="pill pill-slate">7 of 8 checks</span>
              <button type="button" className="btn btn-gold btn-sm" onClick={runTheBurst} disabled={bursting}>
                {bursting ? "Bursting" : burstDone ? "Burst again" : "Send 300 events"}
              </button>
            </div>

            <ol className="phases" aria-label="Burst phases">
              {PHASES.map((p, i) => (
                <li
                  key={p.id}
                  className={`phase${i < phaseIndex ? " phase-done" : ""}${i === phaseIndex ? " phase-now" : ""}`}
                >
                  {p.label}
                </li>
              ))}
            </ol>

            <div className="burst-grid">
              <div className="burst-stat">
                <span className="burst-value">{burst?.posted ?? 0}</span>
                <span className="burst-label">posted</span>
              </div>
              <div className="burst-stat">
                <span className="burst-value">{burst?.accepted ?? 0}</span>
                <span className="burst-label">accepted</span>
              </div>
              <div className="burst-stat">
                <span className="burst-value burst-slate">{burst?.deduplicated ?? 0}</span>
                <span className="burst-label">deduplicated</span>
              </div>
              <div className="burst-stat">
                <span className="burst-value burst-bad">{burst?.rejected ?? 0}</span>
                <span className="burst-label">rejected</span>
              </div>
            </div>

            <div className="bars" aria-label="Deliveries by status">
              {STATUSES.map((s) => {
                const n = burst?.stats?.deliveries[s] ?? 0;
                const pct = totalDeliveries ? (n / totalDeliveries) * 100 : 0;
                return (
                  <div key={s} className="bar-row">
                    <span className="bar-label mono">{s}</span>
                    <span className="bar-track">
                      <motion.span
                        className={`bar-fill bar-${s}`}
                        animate={{ width: `${pct}%` }}
                        transition={{ duration: reduce ? 0 : 0.3 }}
                      />
                    </span>
                    <span className="bar-n mono">{n}</span>
                  </div>
                );
              })}
            </div>

            <div className="burst-smoke mono" aria-live="polite">
              smoke inside the burst:{" "}
              {burst ? `${burst.smoke.filter((r) => r.passed).length}/${burst.smoke.length}` : "0/0"} passed
              {burst?.stats?.latency_ms.p50 != null && (
                <span className="dim">
                  {"  "}p50 {burst.stats.latency_ms.p50} ms, p95 {burst.stats.latency_ms.p95} ms
                </span>
              )}
            </div>

            <AnimatePresence>
              {burstDone && (
                <motion.div
                  className="burst-result"
                  aria-live="polite"
                  initial={reduce ? false : { opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <pre className="code">{burstDone.lines.join("\n")}</pre>
                  <ul className="burst-checks" aria-label="Demo assertions">
                    {burstDone.checks.map((c) => (
                      <li key={c.name} className={c.ok ? "ok" : "bad"}>
                        <span className="mono">{c.ok ? "check ok " : "check BAD"}</span> {c.name}
                      </li>
                    ))}
                  </ul>
                </motion.div>
              )}
            </AnimatePresence>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
