import { useEffect, useState } from "react";
import { runSelfCheck, type SelfCheckLine } from "../sim/selfcheck";

/**
 * Runs the port's own assertions inside the page and shows the tally.
 *
 * Off by default: a visitor should not pay for a full self-check on first paint. It runs on
 * the dev server, or in production when the URL carries `?selfcheck`, and then reports the
 * result on the page instead of only in the console.
 */
export function SelfCheckTally() {
  const [enabled] = useState(() => {
    if (import.meta.env.DEV) return true;
    if (typeof location === "undefined") return false;
    return new URLSearchParams(location.search).has("selfcheck");
  });
  const [lines, setLines] = useState<SelfCheckLine[] | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    runSelfCheck(
      (line) => console.log(`%c[launchbridge]%c ${line}`, "color:#f2c14e", "color:inherit"),
      { burst: false },
    ).then((result) => {
      if (!cancelled) setLines(result);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  if (!enabled) return null;
  const bad = lines === null ? 0 : lines.filter((line) => !line.ok).length;
  return (
    <p className="footer-fine" aria-live="polite">
      {lines === null
        ? "self-check: running"
        : `self-check: ${lines.length - bad} ok, ${bad} bad (full output in the console)`}
    </p>
  );
}
