export function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer-inner">
        <div className="footer-note">
          <p>
            This console is a browser port of LaunchBridge, a FastAPI + PostgreSQL integration
            service shipped with Docker builds, a compose stack, Terraform for AWS (ECS Fargate,
            RDS, ALB, Secrets Manager) and a smoke suite that runs against any base URL. The
            signing, dedup ledger, retry policy, worker and replay logic here follow the Python
            module for module; the database, HTTP transport and clock are in-memory stand-ins.
          </p>
          <p className="footer-fine">
            Timings are virtual and deterministic (seeded PRNG, fixed epoch). Signatures are real
            HMAC-SHA256 from Web Crypto. No network requests leave the page.
          </p>
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
