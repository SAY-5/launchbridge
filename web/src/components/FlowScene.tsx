import { useReducedMotion } from "framer-motion";

/** Node coordinates inside the 960 x 440 viewBox. */
const SOURCES = [
  { name: "orders", x: 80, y: 110 },
  { name: "demo", x: 80, y: 220 },
  { name: "smoke", x: 80, y: 330 },
];
const GATE = { x: 305, y: 220 };
const LEDGER = { x: 520, y: 220 };
const WORKER = { x: 715, y: 220 };
const DESTS = [
  { name: "crm", x: 890, y: 150 },
  { name: "billing", x: 890, y: 290 },
];

function seg(a: { x: number; y: number }, b: { x: number; y: number }, first = false): string {
  const mx = (a.x + b.x) / 2;
  return `${first ? `M ${a.x} ${a.y} ` : ""}C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`;
}

/** Vertical counterpart of seg(), for the stacked layout. */
function vseg(a: { x: number; y: number }, b: { x: number; y: number }, first = false): string {
  const my = (a.y + b.y) / 2;
  return `${first ? `M ${a.x} ${a.y} ` : ""}C ${a.x} ${my}, ${b.x} ${my}, ${b.x} ${b.y}`;
}

/**
 * Stacked layout for narrow screens. The viewBox is 360 wide, so at a 366px content width
 * the scale is about 0.96 and a 13px label renders at roughly 12px.
 */
const T_SOURCES = [
  { name: "orders", x: 62, y: 42 },
  { name: "demo", x: 180, y: 42 },
  { name: "smoke", x: 298, y: 42 },
];
const T_GATE = { x: 180, y: 196 };
const T_LEDGER = { x: 180, y: 384 };
const T_WORKER = { x: 180, y: 572 };
const T_DESTS = [
  { name: "crm", x: 110, y: 762 },
  { name: "billing", x: 250, y: 762 },
];

function tallPath(source: number, dest: number): string {
  return [
    vseg(T_SOURCES[source], T_GATE, true),
    vseg(T_GATE, T_LEDGER),
    vseg(T_LEDGER, T_WORKER),
    vseg(T_WORKER, T_DESTS[dest]),
  ].join(" ");
}

const TALL_PARTICLES: Particle[] = [
  { path: tallPath(0, 0), dur: 6.4, begin: 0, kind: "ok" },
  { path: tallPath(1, 0), dur: 6.1, begin: 1.1, kind: "ok" },
  { path: tallPath(0, 1), dur: 6.6, begin: 2.2, kind: "ok" },
  { path: tallPath(2, 0), dur: 6.2, begin: 3.3, kind: "ok" },
  { path: tallPath(1, 1), dur: 6.3, begin: 4.4, kind: "ok" },
  { path: tallPath(2, 0), dur: 6.5, begin: 5.5, kind: "ok" },
];

function fullPath(source: number, dest: number): string {
  const s = SOURCES[source];
  const d = DESTS[dest];
  return [seg(s, GATE, true), seg(GATE, LEDGER), seg(LEDGER, WORKER), seg(WORKER, d)].join(" ");
}

function rejectPath(source: number): string {
  const s = SOURCES[source];
  return `${seg(s, GATE, true)} C ${GATE.x + 30} ${GATE.y + 60}, ${GATE.x + 40} ${GATE.y + 120}, ${GATE.x + 44} ${GATE.y + 190}`;
}

function dedupPath(source: number): string {
  const s = SOURCES[source];
  return `${seg(s, GATE, true)} ${seg(GATE, LEDGER)} C ${LEDGER.x + 30} ${LEDGER.y + 60}, ${LEDGER.x + 40} ${LEDGER.y + 120}, ${LEDGER.x + 44} ${LEDGER.y + 190}`;
}

type Kind = "ok" | "reject" | "dedup";

interface Particle {
  path: string;
  dur: number;
  begin: number;
  kind: Kind;
}

const PARTICLES: Particle[] = [
  { path: fullPath(0, 0), dur: 6.4, begin: 0, kind: "ok" },
  { path: fullPath(1, 0), dur: 6.1, begin: 0.7, kind: "ok" },
  { path: fullPath(0, 1), dur: 6.6, begin: 1.3, kind: "ok" },
  { path: rejectPath(2), dur: 3.6, begin: 1.9, kind: "reject" },
  { path: fullPath(2, 0), dur: 6.2, begin: 2.4, kind: "ok" },
  { path: dedupPath(1), dur: 4.6, begin: 3.1, kind: "dedup" },
  { path: fullPath(1, 1), dur: 6.3, begin: 3.6, kind: "ok" },
  { path: fullPath(0, 0), dur: 6.0, begin: 4.3, kind: "ok" },
  { path: rejectPath(0), dur: 3.5, begin: 5.0, kind: "reject" },
  { path: fullPath(2, 0), dur: 6.5, begin: 5.5, kind: "ok" },
  { path: dedupPath(0), dur: 4.4, begin: 6.2, kind: "dedup" },
  { path: fullPath(1, 0), dur: 6.2, begin: 6.9, kind: "ok" },
  { path: fullPath(0, 1), dur: 6.4, begin: 7.6, kind: "ok" },
  { path: fullPath(2, 0), dur: 6.1, begin: 8.4, kind: "ok" },
];

const CYCLE = 9.2;

const COLORS: Record<Kind, string> = {
  ok: "var(--gold)",
  reject: "var(--bad)",
  dedup: "var(--slate)",
};

function Node({
  x,
  y,
  w,
  h,
  title,
  sub,
  accent,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <g className={`flow-node${accent ? " flow-node-accent" : ""}`} transform={`translate(${x - w / 2} ${y - h / 2})`}>
      <rect width={w} height={h} rx="12" />
      <text x={w / 2} y={sub ? h / 2 - 4 : h / 2 + 5} textAnchor="middle" className="flow-title">
        {title}
      </text>
      {sub ? (
        <text x={w / 2} y={h / 2 + 15} textAnchor="middle" className="flow-sub">
          {sub}
        </text>
      ) : null}
    </g>
  );
}

function Particles({ particles }: { particles: Particle[] }) {
  return (
    <g filter="url(#flow-glow)">
      {particles.map((p, i) => (
        <g key={i} opacity="0">
          <circle r="4.2" fill={COLORS[p.kind]} />
          <animateMotion
            dur={`${p.dur}s`}
            begin={`${p.begin}s`}
            repeatCount="indefinite"
            path={p.path}
            rotate="auto"
          />
          <animate
            attributeName="opacity"
            values="0;1;1;0;0"
            keyTimes="0;0.05;0.9;1;1"
            dur={`${p.dur}s`}
            begin={`${p.begin}s`}
            repeatCount="indefinite"
          />
        </g>
      ))}
    </g>
  );
}

/** The same pipeline stacked head to tail, shown instead of the wide one under 640px. */
function TallScene({ reduce }: { reduce: boolean | null }) {
  return (
    <svg
      viewBox="0 0 360 880"
      className="flow-svg flow-tall"
      role="img"
      aria-describedby="flow-caption"
    >
      <defs>
        <linearGradient id="flow-line-tall" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="rgba(143,163,191,0.35)" />
          <stop offset="1" stopColor="rgba(242,193,78,0.45)" />
        </linearGradient>
      </defs>
      <g className="flow-rails" fill="none" stroke="url(#flow-line-tall)" strokeWidth="1.4">
        {T_SOURCES.map((s, i) => (
          <path key={`ts${i}`} d={vseg(s, T_GATE, true)} />
        ))}
        <path d={vseg(T_GATE, T_LEDGER, true)} />
        <path d={vseg(T_LEDGER, T_WORKER, true)} />
        {T_DESTS.map((d, i) => (
          <path key={`td${i}`} d={vseg(T_WORKER, d, true)} />
        ))}
      </g>

      <g className="flow-drops" fill="none" strokeDasharray="3 5" strokeWidth="1">
        <path
          d={`M ${T_GATE.x + 150} ${T_GATE.y} C ${T_GATE.x + 170} ${T_GATE.y + 30}, ${T_GATE.x + 170} ${T_GATE.y + 50}, ${T_GATE.x + 150} ${T_GATE.y + 62}`}
          stroke="rgba(239,122,122,0.5)"
        />
        <path
          d={`M ${T_LEDGER.x + 150} ${T_LEDGER.y} C ${T_LEDGER.x + 170} ${T_LEDGER.y + 30}, ${T_LEDGER.x + 170} ${T_LEDGER.y + 50}, ${T_LEDGER.x + 150} ${T_LEDGER.y + 62}`}
          stroke="rgba(143,163,191,0.5)"
        />
      </g>

      {T_SOURCES.map((s) => (
        <Node key={s.name} x={s.x} y={s.y} w={104} h={40} title={s.name} />
      ))}
      <Node
        x={T_GATE.x}
        y={T_GATE.y}
        w={300}
        h={66}
        title="verify"
        sub="timestamp + digest"
        accent
      />
      <text x={T_GATE.x} y={T_GATE.y + 78} textAnchor="middle" className="flow-drop-label flow-drop-bad">
        401 / 409 rejected
      </text>
      <Node
        x={T_LEDGER.x}
        y={T_LEDGER.y}
        w={300}
        h={66}
        title="processed_events"
        sub="UNIQUE (source, key)"
        accent
      />
      <text
        x={T_LEDGER.x}
        y={T_LEDGER.y + 78}
        textAnchor="middle"
        className="flow-drop-label flow-drop-slate"
      >
        200 deduplicated
      </text>
      <Node
        x={T_WORKER.x}
        y={T_WORKER.y}
        w={300}
        h={66}
        title="worker"
        sub="SKIP LOCKED + backoff"
        accent
      />
      {T_DESTS.map((d) => (
        <Node key={d.name} x={d.x} y={d.y} w={124} h={40} title={d.name} />
      ))}
      <text x={T_DESTS[0].x} y={T_DESTS[0].y + 44} className="flow-sub" textAnchor="middle">
        X-Idempotency-Key
      </text>

      {!reduce && <Particles particles={TALL_PARTICLES} />}
      {reduce && (
        <g>
          <circle cx={T_GATE.x} cy={T_GATE.y - 70} r="5" fill={COLORS.ok} />
          <circle cx={T_LEDGER.x} cy={T_LEDGER.y - 70} r="5" fill={COLORS.ok} />
          <circle cx={T_WORKER.x} cy={T_WORKER.y - 70} r="5" fill={COLORS.ok} />
        </g>
      )}
      <text x="8" y="872" className="flow-cycle">
        {reduce ? "static view" : `loop ${CYCLE}s`}
      </text>
    </svg>
  );
}

export function FlowScene() {
  const reduce = useReducedMotion();
  return (
    <figure className="flow glass" aria-labelledby="flow-caption">
      <svg
        viewBox="0 0 960 440"
        className="flow-svg flow-wide"
        role="img"
        aria-describedby="flow-caption"
      >
        <defs>
          <linearGradient id="flow-line" x1="0" x2="1">
            <stop offset="0" stopColor="rgba(143,163,191,0.35)" />
            <stop offset="1" stopColor="rgba(242,193,78,0.45)" />
          </linearGradient>
          <filter id="flow-glow" x="-100%" y="-100%" width="300%" height="300%">
            <feGaussianBlur stdDeviation="4" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <g className="flow-rails" fill="none" stroke="url(#flow-line)" strokeWidth="1.2">
          {SOURCES.map((s, i) => (
            <path key={`s${i}`} d={seg(s, GATE, true)} />
          ))}
          <path d={seg(GATE, LEDGER, true)} />
          <path d={seg(LEDGER, WORKER, true)} />
          {DESTS.map((d, i) => (
            <path key={`d${i}`} d={seg(WORKER, d, true)} />
          ))}
        </g>

        <g className="flow-drops" fill="none" strokeDasharray="3 5" strokeWidth="1">
          <path d={`M ${GATE.x} ${GATE.y + 22} C ${GATE.x + 30} ${GATE.y + 60}, ${GATE.x + 40} ${GATE.y + 120}, ${GATE.x + 44} ${GATE.y + 190}`} stroke="rgba(239,122,122,0.45)" />
          <path d={`M ${LEDGER.x} ${LEDGER.y + 26} C ${LEDGER.x + 30} ${LEDGER.y + 60}, ${LEDGER.x + 40} ${LEDGER.y + 120}, ${LEDGER.x + 44} ${LEDGER.y + 190}`} stroke="rgba(143,163,191,0.45)" />
        </g>
        <text x={GATE.x + 52} y={GATE.y + 196} className="flow-drop-label flow-drop-bad">401 / 409 rejected</text>
        <text x={LEDGER.x + 52} y={LEDGER.y + 196} className="flow-drop-label flow-drop-slate">200 deduplicated</text>

        {SOURCES.map((s) => (
          <Node key={s.name} x={s.x} y={s.y} w={116} h={44} title={s.name} sub="HMAC signed POST" />
        ))}
        <Node x={GATE.x} y={GATE.y} w={150} h={64} title="verify" sub="timestamp + digest" accent />
        <Node x={LEDGER.x} y={LEDGER.y} w={168} h={64} title="processed_events" sub="UNIQUE (source, key)" accent />
        <Node x={WORKER.x} y={WORKER.y} w={150} h={64} title="worker" sub="SKIP LOCKED + backoff" accent />
        {DESTS.map((d) => (
          <Node key={d.name} x={d.x} y={d.y} w={116} h={44} title={d.name} sub="X-Idempotency-Key" />
        ))}

        {!reduce && <Particles particles={PARTICLES} />}
        {reduce && (
          <g>
            <circle cx={190} cy={165} r="4.2" fill={COLORS.ok} />
            <circle cx={620} cy={220} r="4.2" fill={COLORS.ok} />
            <circle cx={GATE.x + 40} cy={GATE.y + 120} r="4.2" fill={COLORS.reject} />
            <circle cx={LEDGER.x + 40} cy={LEDGER.y + 120} r="4.2" fill={COLORS.dedup} />
          </g>
        )}
        <text x="16" y="428" className="flow-cycle">
          {reduce ? "static view" : `loop ${CYCLE}s`}
        </text>
      </svg>
      <TallScene reduce={reduce} />
      <figcaption id="flow-caption" className="flow-caption">
        Signed events enter from named sources, pass the timestamp and digest check, hit the
        PostgreSQL ledger where repeats short-circuit, and are claimed by the worker for signed,
        idempotent delivery. Gold is delivered, slate is deduplicated, red is rejected.
      </figcaption>
    </figure>
  );
}
