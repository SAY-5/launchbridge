/** JSON serialisation that matches the Python service byte for byte. */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

/**
 * A JSON string literal the way Python writes one. `json.dumps` defaults to
 * `ensure_ascii=True`, so every character above U+007E is escaped, and those escaped bytes
 * are what the service signs. JSON.stringify leaves them literal, so they are escaped here;
 * astral characters come out as the same surrogate pair Python emits.
 */
function quote(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (ch) => {
    return "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0");
  });
}

function serialize(value: Json, sortKeys: boolean, itemSep: string, keySep: string): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return quote(value);
  if (Array.isArray(value)) {
    return "[" + value.map((v) => serialize(v, sortKeys, itemSep, keySep)).join(itemSep) + "]";
  }
  const keys = Object.keys(value);
  if (sortKeys) keys.sort();
  const parts = keys.map(
    (k) => quote(k) + keySep + serialize(value[k], sortKeys, itemSep, keySep),
  );
  return "{" + parts.join(itemSep) + "}";
}

/** `json.dumps(value)` with Python's default separators (", " and ": "). */
export function pyDumps(value: Json): string {
  return serialize(value, false, ", ", ": ");
}

/** `json.dumps(value, sort_keys=True, separators=(",", ":"))`, the outbound envelope form. */
export function canonicalJson(value: Json): string {
  return serialize(value, true, ",", ":");
}

export function parseJson(text: string): Json | undefined {
  try {
    return JSON.parse(text) as Json;
  } catch {
    return undefined;
  }
}
