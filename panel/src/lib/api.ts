// API client for the coordinator. The session lives in an HTTP-only cookie; state
// changes echo the CSRF token the server issued at login (double submit).

let csrfToken: string | null = null;

export function setCsrf(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    const msg =
      typeof detail === "string"
        ? detail
        : (detail as { detail?: string })?.detail ?? `request failed (${status})`;
    super(msg);
    this.status = status;
    this.detail = detail;
  }
  get reauthRequired() {
    return this.status === 403 && String(this.message).includes("reauth");
  }
}

type Json = Record<string, unknown> | unknown[] | null;

async function request<T>(method: string, path: string, body?: Json): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const r = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data);
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: Json = {}) => request<T>("POST", path, body),
  put: <T>(path: string, body: Json) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: Json) => request<T>("PATCH", path, body),
};

// --- types (subset of the coordinator's responses) ---------------------------------

export type User = { id?: string; username: string; role: "admin" | "viewer"; totp: boolean;
  disabled?: boolean; last_login_at?: string | null };
export type Me = { user: User; csrf_token: string | null; reauth_valid?: boolean };

export type Overview = {
  at: string;
  jobs: { type: string; status: string; count: number }[];
  nodes: { total: number; online: number };
  fetches_per_hour: { hour: string; fetches: number }[];
  fetches_24h: number;
  domains: { total: number; blocked: number };
  budget: { state: string; spent_month_usd: string; hard_cap_usd: string;
    daily_allowance_usd: string };
  recent_domain_events: { at: string; domain: string; kind: string; reason: string | null }[];
};

export type NodeRow = {
  id: string; name: string; trust_level: string; version: string | null;
  egress_ip: string | null; egress_group: string | null; actual_state: string | null;
  desired_run_state: string; desired_target_version: string | null;
  resources: { cpu_limit?: number; mem_limit_bytes?: number; mem_used_bytes?: number;
    cpu_load?: number };
  concurrency: { http_slots?: number; browser_slots?: number; active_jobs?: number;
    spooled_results?: number };
  agent_status: { running_version?: string; update_state?: string; update_error?: string;
    agent_version?: string } | null;
  last_heartbeat_at: string | null; online: boolean; revoked_at: string | null;
};

export type DomainRow = {
  domain: string; block_status: string; block_reason: string | null; fetch_mode: string;
  robots_status: string | null; robots_crawl_delay_s: number | null; pages_fetched: number;
  last_fetch_at: string | null; overrides: { rate_rps?: number; [block: `block_${string}`]: number | null | undefined }; queued_jobs: number;
  egress_groups: { egress_group: string; rate_rps: number; cooldown_until: string | null;
    throttle_count: number; latency_ewma_ms: number | null }[];
};

export type DomainEvent = { at: string; kind: string; reason: string | null;
  egress_group: string | null; old_rate_rps: number | null; new_rate_rps: number | null };

export type SourceRow = { name: string; type: string; status: string;
  config: Record<string, unknown>; created_at: string };

export type ReleaseRow = { manifest: { version: string; images: Record<string, string>;
  created_at: string }; signature: string; key_id: string; notes: string | null };

export type CategoryPathRow = { source: string; path: string; products: number;
  mapping_id: string | null; taxonomy_node_id: string | null; method: string | null;
  confidence: number | null; node_path: string | null; node_external_id: string | null;
  note: string | null; created_by: string | null };

export type ClassificationStatus = { products: number; unclassified: number; embedded: number;
  generation: number;
  current: { tier: string; version: string; products: number; avg_confidence: number | null;
    below_0_6: number; at_least_0_8: number }[];
  attempts: { tier: string; rows: number; no_category: number }[];
  batches: { stage: string; batches: number; requests: number }[];
  agreement: { tier: string; compared: number; agreed: number }[] };

export type KnnEval = { examples: number; evaluated: number;
  thresholds: { threshold: number; coverage: number; accuracy: number | null }[] };

export type TaxonomyHit = { id: string; external_id: string; path: string;
  local_path: string | null; is_custom: boolean };

export type PriceChecks = {
  flagged: Record<string, number>;
  reviews: { version: string; verdict: string; offers: number }[];
  latest: { offer_id: string; product_id: string | null; title: string | null; source: string;
    url: string | null; price: string | null; currency: string | null; raw_price: string | null;
    flags: string[]; verdict: string; confidence: number | null; reason: string | null;
    version: string; at: string }[];
};
