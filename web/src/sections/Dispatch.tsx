import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useRef, useState } from "react";
import { Reveal } from "../components/Reveal";
import { useConsole } from "../console";
import type { DeliveryAttemptRow, DeliveryRow } from "../sim/models";
import type { WebhookAccepted } from "../sim/service";
import { buildEnvelope, outboundHeaders } from "../sim/worker";
import { fmtMs, shortId, wait } from "../util";

type Scenario = "healthy" | "flaky" | "down" | "broken" | "offline";

const SCENARIOS: { id: Scenario; label: string; blurb: string }[] = [
  { id: "healthy", label: "200 healthy", blurb: "destination answers 200 on the first attempt" },
  { id: "flaky", label: "503 twice, then 200", blurb: "transient failures, two jittered backoffs, then success" },
  { id: "down", label: "503 always", blurb: "transient every time; the budget of 4 attempts runs out" },
  { id: "broken", label: "400 hard failure", blurb: "permanent 4xx: no retry, failed on attempt 1" },
  { id: "offline", label: "connection refused", blurb: "transport error is transient; retried until exhausted" },
];

interface Trace {
  id: number;
  scenario: Scenario;
  delivery: DeliveryRow;
  attempts: DeliveryAttemptRow[];
  headers: Record<string, string>;
  envelope: string;
  receiverCount: number;
  totalMs: number;
}

interface DedupSend {
  id: number;
  status: number;
  deduplicated: boolean;
  event_id: string;
  timestamp: number;
  signature: string;
}

export function Dispatch() {
  const { service, version, bump } = useConsole();
  const reduce = useReducedMotion();

  // dedup panel
  const [eventId, setEventId] = useState("order-77");
  const [sends, setSends] = useState<DedupSend[]>([]);
  const [busy, setBusy] = useState(false);

  // trace panel
  const [scenario, setScenario] = useState<Scenario>("flaky");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [revealed, setRevealed] = useState(0);
  const [tracing, setTracing] = useState(false);
  const traceSerial = useRef(0);

  const sendDedup = async () => {
    setBusy(true);
    service.clock.advance(1000); // a fresh timestamp: a legitimate retry, not a replayed signature
    const timestamp = service.clock.seconds();
    const r = await service.post("orders", { id: eventId, kind: "dedup-demo" });
    const body = r.body as WebhookAccepted;
    const event = service.db.events.get(body.event_id);
    setSends((s) =>
      [
        {
          id: s.length + 1,
          status: r.status,
          deduplicated: body.deduplicated,
          event_id: body.event_id,
          timestamp,
          signature: event?.signature ?? "",
        },
        ...s,
      ].slice(0, 5),
    );
    await service.worker.settle();
    bump();
    setBusy(false);
  };

  const runTrace = async () => {
    setTracing(true);
    setRevealed(0);
    traceSerial.current += 1;
    const tag = `trace-${traceSerial.current}`;
    service.receiver.clearRules();
    if (scenario === "flaky") service.receiver.addRule({ tag, status: 503, times: 2 });
    if (scenario === "down") service.receiver.addRule({ tag, status: 503 });
    if (scenario === "broken") service.receiver.addRule({ tag, status: 400 });
    service.receiver.offline = scenario === "offline";

    service.clock.advance(1000);
    const r = await service.post("demo", { id: `trace-${traceSerial.current}`, tag, kind: "trace" });
    const deliveryId = (r.body as WebhookAccepted).delivery_ids[0];
    const attempts: DeliveryAttemptRow[] = [];
    service.worker.onAttempt = (e) => {
      if (e.delivery.id === deliveryId) attempts.push(e.attempt);
    };
    await service.worker.settle();
    service.worker.onAttempt = undefined;
    service.receiver.offline = false;
    service.receiver.clearRules();

    const delivery = { ...service.db.deliveries.get(deliveryId)! };
    const event = service.db.events.get(delivery.event_id)!;
    const destination = service.registry.get(delivery.destination)!;
    const envelope = buildEnvelope(event, delivery, service.clock.iso(event.received_at));
    const headers = await outboundHeaders(destination, delivery, envelope, Math.floor(attempts[0].started_at / 1000));
    const last = attempts[attempts.length - 1];
    const t: Trace = {
      id: traceSerial.current,
      scenario,
      delivery,
      attempts,
      headers,
      envelope,
      receiverCount: service.receiver.inbox(delivery.idempotency_key)?.count ?? 0,
      totalMs: last.started_at + last.duration_ms - delivery.created_at,
    };
    setTrace(t);
    bump();
    for (let i = 1; i <= attempts.length + 1; i++) {
      await wait(reduce ? 0 : 480);
      setRevealed(i);
    }
    setTracing(false);
  };

  const ledgerRows = [...service.db.processedEvents.values()].filter((p) => p.source === "orders").slice(-6).reverse();
  const eventRows = [...service.db.events.values()]
    .filter((e) => e.source === "orders" && e.event_key === `id:${eventId}`)
    .slice(-6)
    .reverse();
  const liveRow = service.db.ledgerRow("orders", `id:${eventId}`);
  const deliveriesForKey = liveRow ? service.db.deliveriesForEvent(liveRow.event_id) : [];
  const policy = trace ? service.registry.get(trace.delivery.destination)!.retry : null;
  void version;

  return (
    <section className="section" id="dispatch" aria-labelledby="dispatch-title">
      <div className="wrap">
        <Reveal className="section-head">
          <span className="eyebrow">02 / Dedup + dispatch</span>
          <h2 id="dispatch-title">Recorded once. Delivered with a budget.</h2>
          <p>
            The dedup ledger is a PostgreSQL table with <code>UNIQUE (source, event_key)</code>;
            the API inserts with <code>ON CONFLICT DO NOTHING RETURNING id</code>, so a repeat
            short-circuits inside the database. Accepted events fan out to deliveries the worker
            claims with <code>FOR UPDATE SKIP LOCKED</code> and retries with exponential,
            jittered backoff until the budget runs out.
          </p>
        </Reveal>

        <div className="dispatch">
          <Reveal className="glass panel dedup" delay={0.05}>
            <div className="panel-head">
              <h3>Ledger short-circuit</h3>
              <span className="pill pill-slate">processed_events</span>
            </div>
            <div className="dedup-controls">
              <div className="field">
                <label htmlFor="evt">payload id (event key = id:&lt;value&gt;)</label>
                <input
                  id="evt"
                  className="input"
                  value={eventId}
                  onChange={(e) => setEventId(e.target.value)}
                  spellCheck={false}
                />
              </div>
              <button type="button" className="btn btn-gold" onClick={sendDedup} disabled={busy || !eventId}>
                Send to /webhooks/orders
              </button>
            </div>
            <p className="hint">
              Each send uses a fresh timestamp, so the signature is new and the request reaches
              the ledger instead of tripping the replay guard. Send it twice.
            </p>

            <ul className="sends" aria-label="Responses">
              <AnimatePresence initial={false}>
                {sends.map((s) => (
                  <motion.li
                    key={s.id}
                    className={`send-row${s.deduplicated ? " send-dedup" : ""}`}
                    initial={reduce ? false : { opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    transition={{ duration: 0.35 }}
                  >
                    <span className={`pill ${s.deduplicated ? "pill-slate" : "pill-gold"}`}>{s.status}</span>
                    <span className="send-text">
                      {s.deduplicated ? (
                        <>
                          <strong>deduplicated: true</strong>, delivery_ids []
                        </>
                      ) : (
                        <>
                          <strong>deduplicated: false</strong>, {deliveriesForKey.length} deliveries enqueued
                        </>
                      )}
                    </span>
                    <span className="mono send-meta">
                      ts {s.timestamp} sig {s.signature.slice(7, 19)}
                    </span>
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>

            <div className="table-wrap">
              <table className="table">
                <caption className="visually-hidden">Dedup ledger rows for source orders</caption>
                <thead>
                  <tr>
                    <th>source</th>
                    <th>event_key</th>
                    <th>signature</th>
                    <th>event_id</th>
                  </tr>
                </thead>
                <tbody>
                  {ledgerRows.length === 0 && (
                    <tr>
                      <td colSpan={4} className="table-empty">
                        no rows yet
                      </td>
                    </tr>
                  )}
                  {ledgerRows.map((row) => (
                    <tr key={row.id} className={row.event_key === `id:${eventId}` && sends[0]?.deduplicated ? "row-hit" : ""}>
                      <td className="mono">{row.source}</td>
                      <td className="mono">{row.event_key}</td>
                      <td className="mono dim">{row.signature.slice(7, 21)}</td>
                      <td className="mono dim">{shortId(row.event_id)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {eventRows.length > 0 && (
              <div className="events-audit">
                <span className="field-label">events table for this key (kept for audit)</span>
                <ul className="audit-list">
                  {eventRows.map((e) => (
                    <li key={e.id}>
                      <span className={`pill ${e.status === "accepted" ? "pill-gold" : "pill-slate"}`}>{e.status}</span>
                      <span className="mono dim">{shortId(e.id)}</span>
                      <span className="mono dim">signed_at {e.signed_at}</span>
                    </li>
                  ))}
                </ul>
                {deliveriesForKey.length > 0 && (
                  <ul className="audit-list">
                    {deliveriesForKey.map((d) => (
                      <li key={d.id}>
                        <span className={`pill pill-${d.status === "delivered" ? "ok" : d.status === "failed" ? "bad" : "warn"}`}>
                          {d.status}
                        </span>
                        <span className="mono">{d.destination}</span>
                        <span className="mono dim">{d.idempotency_key.slice(0, 8)}:{d.destination}</span>
                        <span className="mono dim">{fmtMs(d.latency_ms)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </Reveal>

          <Reveal className="glass panel trace" delay={0.12}>
            <div className="panel-head">
              <h3>Delivery trace</h3>
              <span className="pill pill-slate">crm: 4 attempts, 0.5s base, x2, jitter 0.2</span>
            </div>
            <div className="scenarios" role="group" aria-label="Destination behaviour">
              {SCENARIOS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  aria-pressed={scenario === s.id}
                  className={`scenario${scenario === s.id ? " scenario-on" : ""}`}
                  onClick={() => setScenario(s.id)}
                >
                  <span className="scenario-label">{s.label}</span>
                  <span className="scenario-blurb">{s.blurb}</span>
                </button>
              ))}
            </div>
            <div className="signer-actions">
              <button type="button" className="btn btn-gold" onClick={runTrace} disabled={tracing}>
                {tracing ? "Dispatching" : "Dispatch one event"}
              </button>
            </div>

            <AnimatePresence mode="wait">
              {trace && policy && (
                <motion.div
                  key={trace.id}
                  className="trace-body"
                  initial={reduce ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={reduce ? undefined : { opacity: 0 }}
                >
                  <div className="track" aria-hidden="true">
                    {trace.attempts.slice(0, revealed).map((a, i) => {
                      const left = ((a.started_at - trace.delivery.created_at) / trace.totalMs) * 100;
                      const width = Math.max((a.duration_ms / trace.totalMs) * 100, 1.2);
                      const backoffWidth = a.backoff_ms ? (a.backoff_ms / trace.totalMs) * 100 : 0;
                      return (
                        <div key={a.id}>
                          <motion.span
                            className={`track-attempt track-${a.outcome}`}
                            style={{ left: `${left}%`, width: `${width}%` }}
                            initial={reduce ? false : { scaleY: 0 }}
                            animate={{ scaleY: 1 }}
                            title={`attempt ${a.attempt_number}: ${a.status_code ?? "transport error"}`}
                          />
                          {a.backoff_ms ? (
                            <motion.span
                              className="track-backoff"
                              style={{ left: `${left + width}%`, width: `${backoffWidth}%` }}
                              initial={reduce ? false : { scaleX: 0 }}
                              animate={{ scaleX: 1 }}
                              transition={{ duration: 0.4, delay: 0.1 }}
                            >
                              <span>{fmtMs(a.backoff_ms)}</span>
                            </motion.span>
                          ) : null}
                          <span className="track-index" style={{ left: `${left}%` }}>
                            {i + 1}
                          </span>
                        </div>
                      );
                    })}
                    <span className="track-zero">0</span>
                    <span className="track-end">{fmtMs(trace.totalMs)}</span>
                  </div>

                  <ol className="attempts" aria-label="Attempts">
                    {trace.attempts.slice(0, revealed).map((a) => {
                      const base = Math.round(policy.baseBackoff(a.attempt_number) * 1000);
                      const pct = a.backoff_ms ? Math.round(((a.backoff_ms - base) / base) * 100) : 0;
                      return (
                        <motion.li
                          key={a.id}
                          className={`attempt attempt-${a.outcome}`}
                          initial={reduce ? false : { opacity: 0, x: -12 }}
                          animate={{ opacity: 1, x: 0 }}
                        >
                          <span className="attempt-n">#{a.attempt_number}</span>
                          <span className={`pill ${a.outcome === "success" ? "pill-ok" : a.outcome === "transient" ? "pill-warn" : "pill-bad"}`}>
                            {a.status_code ?? "no response"}
                          </span>
                          <span className="attempt-outcome mono">{a.outcome}</span>
                          <span className="mono dim">{fmtMs(a.duration_ms)}</span>
                          <span className="attempt-backoff mono">
                            {a.backoff_ms
                              ? `backoff ${fmtMs(a.backoff_ms)} (base ${fmtMs(base)}, ${pct >= 0 ? "+" : ""}${pct}% jitter)`
                              : a.outcome === "success"
                                ? "delivered"
                                : a.outcome === "permanent"
                                  ? "permanent, no retry"
                                  : "budget exhausted"}
                          </span>
                          {a.error && <span className="attempt-error mono">{a.error}</span>}
                        </motion.li>
                      );
                    })}
                  </ol>

                  {revealed > trace.attempts.length && (
                    <motion.div
                      className={`terminal terminal-${trace.delivery.status}`}
                      initial={reduce ? false : { opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                    >
                      <span className={`pill ${trace.delivery.status === "delivered" ? "pill-ok" : "pill-bad"}`}>
                        {trace.delivery.status}
                      </span>
                      <span className="terminal-text">
                        {trace.delivery.status === "delivered"
                          ? `latency ${fmtMs(trace.delivery.latency_ms)} from creation, ${trace.delivery.attempts} attempt${trace.delivery.attempts === 1 ? "" : "s"}`
                          : `${trace.delivery.attempts}/${trace.delivery.max_attempts} attempts, last_error kept for replay`}
                      </span>
                      <span className="mono dim">receiver saw the key {trace.receiverCount} time{trace.receiverCount === 1 ? "" : "s"}</span>
                      {trace.delivery.status === "failed" && (
                        <a className="terminal-link" href="#replay">
                          replay it below
                        </a>
                      )}
                    </motion.div>
                  )}

                  <details className="outbound">
                    <summary>Outbound request the worker signs on every attempt</summary>
                    <pre className="code break">
                      <span className="k">POST</span> {service.registry.get(trace.delivery.destination)?.url}
                      {"\n"}
                      {Object.entries(trace.headers).map(([k, v]) => (
                        <span key={k}>
                          <span className="k">{k}:</span> <span className={k.startsWith("X-") ? "v" : ""}>{v}</span>
                          {"\n"}
                        </span>
                      ))}
                      {"\n"}
                      <span className="s">{trace.envelope}</span>
                    </pre>
                  </details>
                </motion.div>
              )}
            </AnimatePresence>
            {!trace && (
              <p className="verdict-empty">
                Pick a destination behaviour and dispatch. The trace shows every attempt, the
                jitter applied to each backoff, and the terminal state the row ends in.
              </p>
            )}
          </Reveal>
        </div>
      </div>
    </section>
  );
}
