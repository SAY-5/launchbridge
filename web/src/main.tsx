import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/theme.css";
import App from "./App";
import { runSelfCheck } from "./sim/selfcheck";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Console self-check of the port: signing, rejection paths, dedup, bounded retries, replay.
runSelfCheck((line) => console.log(`%c[launchbridge]%c ${line}`, "color:#f2c14e", "color:inherit"), {
  burst: false,
}).then((lines) => {
  const bad = lines.filter((l) => !l.ok).length;
  console.log(`[launchbridge] self-check: ${lines.length - bad} ok, ${bad} bad`);
});
