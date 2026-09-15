import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useRef, useState } from "react";
import { Reveal } from "../components/Reveal";
import { ADMIN, useConsole } from "../console";
import { EPOCH_MS } from "../sim/clock";
import type { DeliveryRow } from "../sim/models";
import { fmtMs, shortId } from "../util";

interface Receipt {
  id: number;
  original: DeliveryRow;
  replacement: DeliveryRow;
  receiverCount: number;
  mode: "single" | "bulk";
}

export function Replay() {
  const { service, version, bump } = useConsole();
  const reduce = useReducedMotion();
  const [seeding, setSeeding] = useState(false);
  const [replaying, setReplaying] = useState(false);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const seedSerial = useRef(0);
  const receiptSerial = useRef(0);
  const offline = service.receiver.offline;
  void version;

  const failed = [...service.db.deliveries.values()]
    .filter((d) => d.status === "failed")
    .sort((a, b) => b.created_at - a.created_at);
  const audit = [...service.db.replays].sort((a, b) => b.id - a.id).slice(0, 8);

  const seed = async () => {
    setSeeding(true);
    service.receiver.offline = true;
    service.clock.advance(1000);
    for (let i = 0; i < 5; i++) {
      seedSerial.current += 1;
      await service.post("demo", { id: `outage-${seedSerial.current}`, kind: "outage" });
      service.clock.advance(15);
    }
    await service.worker.settle();
    bump();
    setSeeding(false);
  };

  const collect = (ids: string[], mode: "single" | "bulk") => {
    const fresh: Receipt[] = ids.map((id) => {
      const replacement = { ...service.db.deliveries.get(id)! };
      const original = { ...service.db.deliveries.get(replacement.replay_of!)! };
      receiptSerial.current += 1;
      return {
        id: receiptSerial.current,
        original,
        replacement,
        receiverCount: service.receiver.inbox(replacement.idempotency_key)?.count ?? 0,
        mode,
      };
    });
    setReceipts((r) => [...fresh.reverse(), ...r].slice(0, 12));
  };

  const replayOne = async (id: string) => {
    setReplaying(true);
    const r = service.replayOne(id, ADMIN, "console: destination repaired");
    if (r.status === 202) {
      const newId = (r.body as { replay_delivery_id: string }).replay_delivery_id;
      await service.worker.settle();
      collect([newId], "single");
    }
    bump();
    setReplaying(false);
  };

  const replayAll = async () => {
    setReplaying(true);
    const r = service.replayBulk({ since: EPOCH_MS, reason: "console: bulk after repair" }, ADMIN);
    if (r.status === 202) {
      const ids = (r.body as { delivery_ids: string[] }).delivery_ids;
      await service.worker.settle();
      collect(ids, "bulk");
    }
    bump();
    setReplaying(false);
  };

  const toggleDestination = () => {
    service.receiver.offline = !service.receiver.offline;
    bump();
  };

  return (
    <section className="section" id="replay" aria-labelledby="replay-title">
      <div className="wrap">
        <Reveal className="section-head">
          <span className="eyebrow">03 / Replay</span>
          <h2 id="replay-title">Failed is a state, not a verdict.</h2>
          <p>
            A replay never edits history. It opens a new delivery row with <code>series + 1</code>,
            a fresh attempt budget, <code>replay_of</code> pointing at the original, and the
            same <code>X-Idempotency-Key</code>, so a destination that stores keys sees one
            event, not two. The original becomes <code>replayed</code> and an audit row records
            who asked and why.
          </p>
        </Reveal>

        <div className="replay">
          <Reveal className="glass panel" delay={0.05}>
            <div className="panel-head">
              <h3>Failed deliveries</h3>
              <span className="pill pill-slate">GET /deliveries?status=failed</span>
            </div>

            <div className="dest-state">
              <div>
                <span className="field-label">destination crm</span>
                <p className={`dest-text ${offline ? "dest-down" : "dest-up"}`}>
                  {offline ? "connection refused" : "reachable, answering 200"}
                </p>
              </div>
              <button
                type="button"
                className={`switch${offline ? "" : " switch-on"}`}
                role="switch"
                aria-checked={!offline}
                onClick={toggleDestination}
              >
                <span className="switch-knob" />
                <span className="switch-text">{offline ? "repair" : "repaired"}</span>
              </button>
            </div>

            <div className="signer-actions">
              <button type="button" className="btn" onClick={seed} disabled={seeding}>
                {seeding ? "Failing 5 deliveries" : "Simulate an outage: 5 events, destination down"}
              </button>
              <button
                type="button"
                className="btn btn-gold"
                onClick={replayAll}
                disabled={replaying || failed.length === 0}
              >
                Replay all ({failed.length}) via POST /replay
              </button>
            </div>
            <p className="hint">
              Replaying while the destination is still down burns another budget and ends
              failed again, with series 2. Flip the switch first.
            </p>

            <ul className="failed-list" aria-label="Failed deliveries">
              <AnimatePresence initial={false}>
                {failed.length === 0 && (
                  <motion.li key="none" className="failed-empty" initial={false}>
                    nothing failed right now
                  </motion.li>
                )}
                {failed.map((d) => (
                  <motion.li
                    key={d.id}
                    className="failed-row"
                    layout={!reduce}
                    initial={reduce ? false : { opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={reduce ? undefined : { opacity: 0, x: 24 }}
                  >
                    <span className="pill pill-bad">failed</span>
                    <span className="failed-main">
                      <span className="mono">{d.destination}</span>
                      <span className="mono dim"> {shortId(d.id)} </span>
                      <span className="mono dim">series {d.series}</span>
                    </span>
                    <span className="mono failed-err">
                      {d.attempts}/{d.max_attempts} attempts, {d.last_error?.slice(0, 60)}
                    </span>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => replayOne(d.id)}
                      disabled={replaying}
                    >
                      Replay
                    </button>
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>
          </Reveal>

          <Reveal className="replay-side" delay={0.12}>
            <div className="glass panel">
              <div className="panel-head">
                <h3>Receipts</h3>
                <span className="pill pill-slate">same key, delivered once</span>
              </div>
              {receipts.length === 0 ? (
                <p className="verdict-empty">
                  Replay something. Each receipt pairs the original row with its series 2
                  replacement and shows the receiver's count for the shared idempotency key.
                </p>
              ) : (
                <ul className="receipts">
                  <AnimatePresence initial={false}>
                    {receipts.map((r) => (
                      <motion.li
                        key={r.id}
                        className={`receipt receipt-${r.replacement.status}`}
                        initial={reduce ? false : { opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                      >
                        <div className="receipt-key">
                          <span className="field-label">X-Idempotency-Key</span>
                          <span className="mono">{r.replacement.idempotency_key}</span>
                        </div>
                        <div className="receipt-pair">
                          <div>
                            <span className="pill pill-warn">replayed</span>
                            <span className="mono dim"> series {r.original.series}, {shortId(r.original.id)}</span>
                          </div>
                          <span className="receipt-arrow" aria-hidden="true">
                            &rarr;
                          </span>
                          <div>
                            <span className={`pill ${r.replacement.status === "delivered" ? "pill-ok" : "pill-bad"}`}>
                              {r.replacement.status}
                            </span>
                            <span className="mono dim">
                              {" "}
                              series {r.replacement.series}, {shortId(r.replacement.id)}
                              {r.replacement.status === "delivered" ? `, ${fmtMs(r.replacement.latency_ms)}` : ""}
                            </span>
                          </div>
                        </div>
                        <span className="receipt-foot mono">
                          {r.mode} replay, receiver count {r.receiverCount}
                          {r.replacement.status === "delivered"
                            ? ", accepted exactly once"
                            : ", still failing: repair the destination"}
                        </span>
                      </motion.li>
                    ))}
                  </AnimatePresence>
                </ul>
              )}
            </div>

            {audit.length > 0 && (
              <div className="glass panel">
                <div className="panel-head">
                  <h3>Audit trail</h3>
                  <span className="pill pill-slate">GET /replays</span>
                </div>
                <div className="table-wrap">
                  <table className="table">
                    <caption className="visually-hidden">Replay audit rows</caption>
                    <thead>
                      <tr>
                        <th>actor</th>
                        <th>mode</th>
                        <th>original</th>
                        <th>new</th>
                        <th>reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {audit.map((r) => (
                        <tr key={r.id}>
                          <td className="mono">{r.actor}</td>
                          <td className="mono">{r.mode}</td>
                          <td className="mono dim">{shortId(r.original_delivery_id)}</td>
                          <td className="mono dim">{shortId(r.new_delivery_id)}</td>
                          <td className="dim">{r.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </Reveal>
        </div>
      </div>
    </section>
  );
}
