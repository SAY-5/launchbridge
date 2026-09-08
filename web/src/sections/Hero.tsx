import { motion, useReducedMotion } from "framer-motion";
import { Counter } from "../components/Counter";
import { FlowScene } from "../components/FlowScene";

const STATS = [
  { label: "events received", value: 300, note: "250 unique, 50 re-sent" },
  { label: "deduplicated", value: 50, note: "ledger short-circuits" },
  { label: "left failed after replay", value: 0, note: "20 failed, 20 replayed" },
  { label: "smoke checks", value: 14, note: "of 14, against the stack", suffix: "/14" },
];

export function Hero() {
  const reduce = useReducedMotion();
  const item = (i: number) =>
    reduce
      ? {}
      : {
          initial: { opacity: 0, y: 26 },
          animate: { opacity: 1, y: 0 },
          transition: { duration: 0.8, delay: 0.1 + i * 0.12, ease: [0.22, 1, 0.36, 1] as const },
        };

  return (
    <section className="hero" id="top" aria-labelledby="hero-title">
      <div className="wrap">
        <motion.p className="eyebrow" {...item(0)}>
          Integration service, browser port
        </motion.p>
        <motion.h1 id="hero-title" className="hero-title" {...item(1)}>
          Every event <span className="hero-gold">signed</span>,{" "}
          <span className="hero-slate">deduplicated</span>, and never lost.
        </motion.h1>
        <motion.p className="hero-lead" {...item(2)}>
          LaunchBridge takes HMAC-signed webhooks in, records each one exactly once in
          PostgreSQL, delivers with bounded retries and jittered backoff, replays what failed
          under the same idempotency key, and signs everything on the way out. This console runs
          a faithful TypeScript port of the service so you can drive every path yourself.
        </motion.p>

        <motion.ul className="hero-stats" aria-label="Demo run headline numbers" {...item(3)}>
          {STATS.map((s, i) => (
            <li key={s.label} className="stat glass">
              <span className="stat-value">
                <Counter value={s.value} delay={0.5 + i * 0.15} />
                {s.suffix ? <span className="stat-suffix">{s.suffix}</span> : null}
              </span>
              <span className="stat-label">{s.label}</span>
              <span className="stat-note">{s.note}</span>
            </li>
          ))}
        </motion.ul>

        <motion.div {...item(4)}>
          <FlowScene />
        </motion.div>
      </div>
    </section>
  );
}
