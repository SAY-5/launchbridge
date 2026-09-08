/** Destination registry, the in-browser reading of destinations.yaml. */

import { RetryPolicy } from "./retry";

export interface Destination {
  name: string;
  url: string;
  secret: string;
  sources: string[];
  retry: RetryPolicy;
}

export class DestinationRegistry {
  readonly destinations: Destination[];

  constructor(destinations: Destination[]) {
    this.destinations = destinations;
  }

  get(name: string): Destination | undefined {
    return this.destinations.find((d) => d.name === name);
  }

  forSource(source: string): Destination[] {
    return this.destinations.filter((d) => d.sources.includes("*") || d.sources.includes(source));
  }

  /** The two destinations shipped in destinations.yaml. */
  static default(receiverUrl = "http://receiver:8081"): DestinationRegistry {
    return new DestinationRegistry([
      {
        name: "crm",
        url: `${receiverUrl}/hooks/crm`,
        secret: "crm-dev-secret",
        sources: ["*"],
        retry: new RetryPolicy({
          maxAttempts: 4,
          baseDelaySeconds: 0.5,
          maxDelaySeconds: 8,
          multiplier: 2,
          jitter: 0.2,
          timeoutSeconds: 5,
        }),
      },
      {
        name: "billing",
        url: `${receiverUrl}/hooks/billing`,
        secret: "billing-dev-secret",
        sources: ["orders"],
        retry: new RetryPolicy({
          maxAttempts: 6,
          baseDelaySeconds: 1,
          maxDelaySeconds: 30,
          timeoutSeconds: 10,
        }),
      },
    ]);
  }
}
