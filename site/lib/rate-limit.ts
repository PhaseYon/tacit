// Per-IP token bucket, in memory. Each serverless instance has its own buckets, so the
// effective limit is per instance; that's fine for a demo whose queries cost nothing.

type Bucket = { tokens: number; updated: number };

const CAPACITY = 10; // burst
const REFILL_PER_SEC = 10 / 60; // 10 queries per minute sustained
const MAX_KEYS = 10_000;

const buckets = new Map<string, Bucket>();

export function take(key: string, now = Date.now()): { ok: boolean; retryAfter: number } {
  let b = buckets.get(key);
  if (!b) {
    if (buckets.size >= MAX_KEYS) prune(now);
    b = { tokens: CAPACITY, updated: now };
    buckets.set(key, b);
  }
  const elapsed = (now - b.updated) / 1000;
  b.tokens = Math.min(CAPACITY, b.tokens + elapsed * REFILL_PER_SEC);
  b.updated = now;
  if (b.tokens >= 1) {
    b.tokens -= 1;
    return { ok: true, retryAfter: 0 };
  }
  return { ok: false, retryAfter: Math.ceil((1 - b.tokens) / REFILL_PER_SEC) };
}

function prune(now: number) {
  const full = (CAPACITY / REFILL_PER_SEC) * 1000;
  for (const [k, b] of buckets) {
    if (now - b.updated > full) buckets.delete(k);
  }
  // Still full (an attack from many IPs): drop the oldest half.
  if (buckets.size >= MAX_KEYS) {
    let i = 0;
    for (const k of buckets.keys()) {
      if (i++ >= MAX_KEYS / 2) break;
      buckets.delete(k);
    }
  }
}

export function clientIp(headers: Headers): string {
  const fwd = headers.get("x-forwarded-for");
  if (fwd) return fwd.split(",")[0].trim();
  return headers.get("x-real-ip") ?? "unknown";
}
