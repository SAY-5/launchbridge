/** Seeded PRNG (mulberry32). The console never calls Math.random. */
export class Prng {
  private state: number;

  constructor(seed: number) {
    this.state = seed >>> 0;
  }

  next(): number {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  /** Uniform float in [a, b], mirroring random.uniform. */
  uniform(a: number, b: number): number {
    return a + (b - a) * this.next();
  }

  /** Integer in [a, b] inclusive. */
  int(a: number, b: number): number {
    return a + Math.floor(this.next() * (b - a + 1));
  }

  hex(length: number): string {
    let out = "";
    while (out.length < length) {
      out += Math.floor(this.next() * 16).toString(16);
    }
    return out.slice(0, length);
  }

  /** RFC 4122 shaped identifier, deterministic per seed. */
  uuid(): string {
    const h = this.hex(32).split("");
    h[12] = "4";
    h[16] = "89ab"[Math.floor(this.next() * 4)];
    const s = h.join("");
    return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
  }
}
