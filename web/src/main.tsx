import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/theme.css";
import App from "./App";

// The port's self-check is not run here: it is gated and reported by SelfCheckTally in the
// footer, so a first paint in production does not pay for it.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
