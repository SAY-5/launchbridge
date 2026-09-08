import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { Service } from "./sim/service";

interface ConsoleValue {
  /** One shared in-process service for the interactive sections. */
  service: Service;
  /** Bumped after every mutation so views re-read the database. */
  version: number;
  bump: () => void;
}

const ConsoleContext = createContext<ConsoleValue | null>(null);

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [service] = useState(() => new Service({ seed: 0x1a7c }));
  const [version, setVersion] = useState(0);
  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const value = useMemo(() => ({ service, version, bump }), [service, version, bump]);
  return <ConsoleContext.Provider value={value}>{children}</ConsoleContext.Provider>;
}

export function useConsole(): ConsoleValue {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error("useConsole must be used inside ConsoleProvider");
  return value;
}

export const ADMIN = { "X-API-Key": "dev-admin-key" };
