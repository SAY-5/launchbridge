import "./styles/console.css";
import { ConsoleProvider } from "./console";
import { Nav } from "./components/Nav";
import { Hero } from "./sections/Hero";
import { SignedIn } from "./sections/SignedIn";
import { Dispatch } from "./sections/Dispatch";

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
      </main>
    </ConsoleProvider>
  );
}
