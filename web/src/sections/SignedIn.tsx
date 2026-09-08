import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { Reveal } from "../components/Reveal";
import { useConsole } from "../console";
import type { ApiResponse, ErrorBody, WebhookAccepted } from "../sim/service";
import { computeSignature, signedMessage } from "../sim/signing";
import { shortId } from "../util";

type Source = "orders" | "demo" | "smoke";
const SOURCES: Source[] = ["orders", "demo", "smoke"];
const DEFAULT_BODY = '{"id": "order-1", "amount": 42}';

interface SentRequest {
  source: string;
  body: string;
  headers: Record<string, string>;
  secretUsed: string;
  signedBody: string;
}

interface Verdict {
  id: number;
  request: SentRequest;
  response: ApiResponse<WebhookAccepted | ErrorBody>;
  now: number;
  reason: string | null;
  message: string | null;
}

type StepState = "pass" | "fail" | "skip" | "dedup";

interface Step {
  label: string;
  detail: string;
  state: StepState;
}

function reasonOf(body: WebhookAccepted | ErrorBody): [string | null, string | null] {
  if ("detail" in body) {
    if (typeof body.detail === "string") return [body.detail, null];
    return [body.detail.error, body.detail.message];
  }
  return [null, null];
}

function stepsFor(v: Verdict, tolerance: number): Step[] {
  const ts = Number.parseInt(v.request.headers["X-Timestamp"] ?? "", 10);
  const diff = Number.isFinite(ts) ? Math.abs(v.now - ts) : NaN;
  const failAt: Record<string, number> = {
    missing_timestamp: 0,
    invalid_timestamp: 0,
    stale_timestamp: 1,
    missing_signature: 2,
    invalid_signature: 2,
    replayed_signature: 3,
  };
  const failing = v.reason && v.reason in failAt ? failAt[v.reason] : -1;
  const state = (i: number): StepState => {
    if (failing === -1) return "pass";
    if (i < failing) return "pass";
    if (i === failing) return "fail";
    return "skip";
  };
  const accepted = v.response.body as WebhookAccepted;
  const dedup = v.response.status === 200 && accepted.deduplicated;
  const steps: Step[] = [
    {
      label: "X-Timestamp present and integer",
      detail: Number.isFinite(ts) ? `${ts}` : "missing or not unix seconds",
      state: state(0),
    },
    {
      label: `|now - timestamp| <= ${tolerance}s`,
      detail: Number.isFinite(diff) ? `|${v.now} - ${ts}| = ${diff}s` : "not evaluated",
      state: state(1),
    },
    {
      label: "HMAC-SHA256 over timestamp.body matches (constant-time compare)",
      detail:
        state(2) === "fail"
          ? "expected digest differs from X-Signature"
          : state(2) === "pass"
            ? "digest matches"
            : "not evaluated",
      state: state(2),
    },
    {
      label: "signature never accepted before (UNIQUE processed_events.signature)",
      detail:
        state(3) === "fail"
          ? "signature already in the ledger, 409"
          : state(3) === "pass"
            ? "first time this signature is seen"
            : "not evaluated",
      state: state(3),
    },
    {
      label: "INSERT processed_events ON CONFLICT (source, event_key) DO NOTHING",
      detail:
        failing !== -1
          ? "not reached"
          : dedup
            ? "conflict on (source, event_key): recorded as deduplicated, no deliveries"
            : `inserted, ${accepted.delivery_ids.length} deliver${accepted.delivery_ids.length === 1 ? "y" : "ies"} enqueued`,
      state: failing !== -1 ? "skip" : dedup ? "dedup" : "pass",
    },
  ];
  return steps;
}

export function SignedIn() {
  const { service, version, bump } = useConsole();
  const reduce = useReducedMotion();
  const [source, setSource] = useState<Source>("orders");
  const [body, setBody] = useState(DEFAULT_BODY);
  const [wrongSecret, setWrongSecret] = useState(false);
  const [customSecret, setCustomSecret] = useState<string | null>(null);
  const [tsOffset, setTsOffset] = useState(0);
  const [tamperBody, setTamperBody] = useState(false);
  const [signature, setSignature] = useState("");
  const [lastRequest, setLastRequest] = useState<SentRequest | null>(null);
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [history, setHistory] = useState<Verdict[]>([]);
  const [sending, setSending] = useState(false);

  const nowSeconds = service.clock.seconds();
  const realSecret = service.secrets[source];
  const secret = customSecret ?? (wrongSecret ? "not-the-secret" : realSecret);
  const timestamp = nowSeconds + tsOffset;
  const message = signedMessage(timestamp, body);
  const sentBody = useMemo(
    () => (tamperBody ? body.replace(/\d/, (d) => String((Number(d) + 1) % 10)) : body),
    [body, tamperBody],
  );

  useEffect(() => {
    let cancelled = false;
    computeSignature(secret, timestamp, body).then((sig) => {
      if (!cancelled) setSignature(sig);
    });
    return () => {
      cancelled = true;
    };
  }, [secret, timestamp, body, version]);

  const send = async (replay = false) => {
    setSending(true);
    let request: SentRequest;
    if (replay && lastRequest) {
      request = lastRequest;
    } else {
      request = {
        source,
        body: sentBody,
        signedBody: body,
        secretUsed: secret,
        headers: {
          "Content-Type": "application/json",
          "X-Timestamp": String(timestamp),
          "X-Signature": await computeSignature(secret, timestamp, body),
        },
      };
    }
    const response = await service.webhook(request.source, request.body, request.headers);
    const [reason, msg] = reasonOf(response.body);
    const v: Verdict = {
      id: (verdict?.id ?? 0) + 1,
      request,
      response,
      now: service.clock.seconds(),
      reason,
      message: msg,
    };
    setVerdict(v);
    setHistory((h) => [v, ...h].slice(0, 6));
    if (!replay) setLastRequest(request);
    bump();
    setSending(false);
  };

  const steps = verdict ? stepsFor(verdict, service.toleranceSeconds) : [];
  const status = verdict?.response.status;
  const tone =
    status === 202 ? "gold" : status === 200 ? "slate" : status === 409 ? "warn" : status ? "bad" : "";

  return (
    <section className="section" id="signed-in" aria-labelledby="signed-in-title">
      <div className="wrap">
        <Reveal className="section-head">
          <span className="eyebrow">01 / Signed in</span>
          <h2 id="signed-in-title">Sign a request live, then break it on purpose.</h2>
          <p>
            The API signs nothing for you. A source computes HMAC-SHA256 over{" "}
            <code>timestamp.body</code> with its shared secret and sends the digest in{" "}
            <code>X-Signature</code>. Tamper with any input and watch the verifier name the exact
            reason it refused.
          </p>
        </Reveal>

        <div className="signer">
          <Reveal className="signer-builder glass" delay={0.05}>
            <div className="panel-head">
              <h3>Request builder</h3>
              <span className="pill pill-slate">virtual now {nowSeconds}</span>
            </div>
            <div className="signer-form">
              <div className="field">
                <label htmlFor="src">source</label>
                <select
                  id="src"
                  className="select"
                  value={source}
                  onChange={(e) => {
                    setSource(e.target.value as Source);
                    setCustomSecret(null);
                  }}
                >
                  {SOURCES.map((s) => (
                    <option key={s} value={s}>
                      POST /webhooks/{s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="secret">secret used to sign</label>
                <input
                  id="secret"
                  className={`input${secret !== realSecret ? " input-bad" : ""}`}
                  value={secret}
                  onChange={(e) => setCustomSecret(e.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                />
              </div>
              <div className="field field-wide">
                <label htmlFor="body">body (raw bytes are what gets signed)</label>
                <textarea
                  id="body"
                  className="textarea"
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  spellCheck={false}
                />
              </div>
            </div>

            <div className="tamper" role="group" aria-label="Tamper with the request">
              <span className="field-label">tamper</span>
              <button
                type="button"
                className={`chip${tamperBody ? " chip-on" : ""}`}
                aria-pressed={tamperBody}
                onClick={() => setTamperBody((v) => !v)}
              >
                flip a byte after signing
              </button>
              <button
                type="button"
                className={`chip${wrongSecret && customSecret === null ? " chip-on" : ""}`}
                aria-pressed={wrongSecret && customSecret === null}
                onClick={() => {
                  setCustomSecret(null);
                  setWrongSecret((v) => !v);
                }}
              >
                sign with the wrong secret
              </button>
              <button
                type="button"
                className={`chip${tsOffset === -3600 ? " chip-on" : ""}`}
                aria-pressed={tsOffset === -3600}
                onClick={() => setTsOffset((v) => (v === -3600 ? 0 : -3600))}
              >
                stale timestamp (now - 3600s)
              </button>
              <button
                type="button"
                className={`chip${tsOffset === 299 ? " chip-on" : ""}`}
                aria-pressed={tsOffset === 299}
                onClick={() => setTsOffset((v) => (v === 299 ? 0 : 299))}
              >
                skew inside the window (+299s)
              </button>
              <button
                type="button"
                className="chip"
                onClick={() => {
                  service.clock.advance(301_000);
                  bump();
                }}
              >
                advance virtual clock +301s
              </button>
            </div>

            <div className="signer-hmac">
              <span className="field-label">HMAC input: timestamp + "." + body</span>
              <pre className="code break">
                <span className="v">{timestamp}</span>
                <span className="d">.</span>
                <span className="s">{body}</span>
              </pre>
              <span className="field-label">headers sent</span>
              <pre className="code break">
                <span className="k">POST</span> /webhooks/{source} <span className="d">HTTP/1.1</span>
                {"\n"}
                <span className="k">Content-Type:</span> application/json{"\n"}
                <span className="k">X-Timestamp:</span> <span className="v">{timestamp}</span>
                {"\n"}
                <span className="k">X-Signature:</span> <span className="v">{signature || "computing"}</span>
                {tamperBody ? (
                  <>
                    {"\n\n"}
                    <span className="d">body on the wire (byte flipped after signing):</span>
                    {"\n"}
                    <span className="bad">{sentBody}</span>
                  </>
                ) : null}
              </pre>
              <p className="hint">
                Signed as <code>{message.length}</code> bytes with secret{" "}
                <code>{secret}</code>. The digest changes with every character above.
              </p>
            </div>

            <div className="signer-actions">
              <button type="button" className="btn btn-gold" onClick={() => send(false)} disabled={sending}>
                Send signed request
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => send(true)}
                disabled={sending || !lastRequest}
                title="Re-send the previous request byte for byte, headers included"
              >
                Replay the last request
              </button>
            </div>
          </Reveal>

          <Reveal className="signer-verdict" delay={0.12}>
            <div className="glass verdict">
              <div className="panel-head">
                <h3>Verifier</h3>
                <span className="pill">launchbridge/signing.py</span>
              </div>
              <AnimatePresence mode="wait" initial={false}>
                {verdict ? (
                  <motion.div
                    key={verdict.id}
                    initial={reduce ? false : { opacity: 0, y: 12 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={reduce ? undefined : { opacity: 0, y: -8 }}
                    transition={{ duration: 0.35 }}
                  >
                    <div className={`verdict-status verdict-${tone}`}>
                      <span className="verdict-code">{status}</span>
                      <span className="verdict-reason">
                        {verdict.reason ??
                          ((verdict.response.body as WebhookAccepted).deduplicated
                            ? "deduplicated"
                            : "accepted")}
                      </span>
                      <span className="verdict-msg">
                        {verdict.message ??
                          (status === 202
                            ? `event ${shortId((verdict.response.body as WebhookAccepted).event_id)} recorded, ${(verdict.response.body as WebhookAccepted).delivery_ids.length} deliveries enqueued`
                            : "same (source, event key) already in the ledger; no deliveries created")}
                      </span>
                    </div>
                    <ol className="steps" aria-label="Verification steps">
                      {steps.map((s, i) => (
                        <motion.li
                          key={`${verdict.id}-${i}`}
                          className={`step step-${s.state}`}
                          initial={reduce ? false : { opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          transition={{ delay: reduce ? 0 : 0.08 * i, duration: 0.3 }}
                        >
                          <span className="step-mark" aria-hidden="true" />
                          <span className="step-text">
                            <span className="step-label">{s.label}</span>
                            <span className="step-detail">{s.detail}</span>
                          </span>
                          <span className="visually-hidden">{s.state}</span>
                        </motion.li>
                      ))}
                    </ol>
                  </motion.div>
                ) : (
                  <motion.p key="empty" className="verdict-empty">
                    Nothing verified yet. Send the request as is for a <code>202</code>, send it
                    twice for a <code>200 deduplicated</code>, replay it for a{" "}
                    <code>409 replayed_signature</code>, or tamper first for a <code>401</code>.
                  </motion.p>
                )}
              </AnimatePresence>
            </div>

            {history.length > 0 && (
              <ul className="history" aria-label="Recent requests">
                {history.map((h) => (
                  <li key={h.id} className="history-row">
                    <span className={`pill pill-${h.response.status === 202 ? "gold" : h.response.status === 200 ? "slate" : "bad"}`}>
                      {h.response.status}
                    </span>
                    <span className="mono history-reason">
                      {h.reason ?? ((h.response.body as WebhookAccepted).deduplicated ? "deduplicated" : "accepted")}
                    </span>
                    <span className="mono history-sig">{h.request.headers["X-Signature"]?.slice(7, 23)}</span>
                    <span className="mono history-ts">ts {h.request.headers["X-Timestamp"]}</span>
                  </li>
                ))}
              </ul>
            )}
          </Reveal>
        </div>
      </div>
    </section>
  );
}
