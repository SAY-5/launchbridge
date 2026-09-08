import "./styles/console.css";
import { ConsoleProvider } from "./console";
import { Nav } from "./components/Nav";
import { Hero } from "./sections/Hero";
import { SignedIn } from "./sections/SignedIn";
import { Dispatch } from "./sections/Dispatch";
import { Replay } from "./sections/Replay";
import { SmokeRun } from "./sections/SmokeRun";
import { Footer } from "./sections/Footer";

export default function App() {
  return (
    <ConsoleProvider>
      <a className="skip-link" href="#signed-in">
        Skip to the interactive console
      </a>
      <Nav />
      <main>
        <Hero />
        <SignedIn />
        <Dispatch />
        <Replay />
        <SmokeRun />
      </main>
      <Footer />
    </ConsoleProvider>
  );
}
