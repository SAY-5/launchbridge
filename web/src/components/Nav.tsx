const LINKS = [
  ["#signed-in", "Signed in"],
  ["#dispatch", "Dedup + dispatch"],
  ["#replay", "Replay"],
  ["#smoke", "Smoke run"],
] as const;

export function Nav() {
  return (
    <header className="nav">
      <div className="wrap nav-inner">
        <a className="brand" href="#top" aria-label="LaunchBridge console, back to top">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 64 64" width="22" height="22">
              <path d="M14 44 32 12l18 32H38l-6-11-6 11z" fill="currentColor" />
              <rect x="26" y="46" width="12" height="6" rx="3" fill="#8fa3bf" />
            </svg>
          </span>
          <span className="brand-name">LaunchBridge</span>
          <span className="brand-sub">console</span>
        </a>
        <nav aria-label="Sections">
          <ul className="nav-links">
            {LINKS.map(([href, label]) => (
              <li key={href}>
                <a href={href}>{label}</a>
              </li>
            ))}
          </ul>
        </nav>
        <a
          className="nav-repo"
          href="https://github.com/SAY-5/launchbridge"
          target="_blank"
          rel="noreferrer"
        >
          <span className="live-dot" aria-hidden="true" />
          Source
        </a>
      </div>
    </header>
  );
}
