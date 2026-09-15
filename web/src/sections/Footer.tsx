import { SelfCheckTally } from "../components/SelfCheckTally";

export function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer-inner">
        <div className="footer-note">
          <p>
            This console is a browser port of LaunchBridge, a FastAPI + PostgreSQL integration
            service shipped with Docker builds, a compose stack, Terraform for AWS (ECS Fargate,
            RDS, ALB, Secrets Manager) and a smoke suite that runs against any base URL. The
            signing, nonce store, dedup ledger, retry policy, worker and replay code here is a
            port of the matching Python modules; the database, HTTP transport and clock are
            in-memory stand-ins. Routing, payload transforms, rate limits, circuit breakers,
            secret rotation and the operations overview are not ported, so the billing
            destination here receives the raw payload rather than the transformed one the
            service sends.
          </p>
          <p className="footer-fine">
            Timings are virtual and deterministic (seeded PRNG, fixed epoch), so a duration
            shown here was computed rather than measured. Signatures are real HMAC-SHA256 from
            Web Crypto, and the signing and JSON output are checked against vectors generated
            by the Python test suite. No network requests leave the page.
          </p>
          <SelfCheckTally />
        </div>
        <ul className="footer-links">
          <li>
            <a href="https://github.com/SAY-5/launchbridge" target="_blank" rel="noreferrer">
              github.com/SAY-5/launchbridge
            </a>
          </li>
          <li>
            <a href="https://github.com/SAY-5/launchbridge/blob/main/ARCHITECTURE.md" target="_blank" rel="noreferrer">
              ARCHITECTURE.md
            </a>
          </li>
          <li>
            <a href="https://github.com/SAY-5/launchbridge/blob/main/smoke/smoke.py" target="_blank" rel="noreferrer">
              smoke/smoke.py
            </a>
          </li>
        </ul>
      </div>
    </footer>
  );
}
