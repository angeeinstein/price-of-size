import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError, type DomainEvent, type DomainRow } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";
import { fmtAgo } from "@/lib/utils";

const BLOCK_FIELDS: [string, string][] = [
  ["block_challenge_run", "Challenges before block"],
  ["block_retry_after_h", "Retry after (h, 0 = never)"],
  ["block_forbidden_run", "403s before block"],
];

function BlockOverrides({ d, save }: { d: DomainRow; save: (body: Record<string, unknown>) => void }) {
  const current = (k: string) => {
    const o = d.overrides as Record<string, number | null | undefined>;
    if (!(k in o)) return "";
    return o[k] === null ? "0" : String(o[k]);
  };
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(BLOCK_FIELDS.map(([k]) => [k, current(k)])));
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted">Block policy for this domain (empty: global setting)</div>
      {BLOCK_FIELDS.map(([k, label]) => (
        <label key={k} className="flex items-center gap-2 text-xs">
          <Input className="h-7 w-20" value={values[k]} aria-label={label}
            onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
          {label}
        </label>
      ))}
      <Button size="sm" variant="outline" onClick={() => save({
        block: Object.fromEntries(BLOCK_FIELDS.map(([k]) => [k, values[k] === "" ? null : Number(values[k])])),
      })}>Save block policy</Button>
    </div>
  );
}

function Detail({ d }: { d: DomainRow }) {
  const qc = useQueryClient();
  const admin = useIsAdmin();
  const { sensitive } = useAuth();
  const [rate, setRate] = useState(d.overrides.rate_rps?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const events = useQuery({ queryKey: ["domain-events", d.domain], queryFn: () =>
    api.get<DomainEvent[]>(`/api/admin/domains/${encodeURIComponent(d.domain)}/events?limit=50`) });
  const update = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      sensitive(() => api.patch(`/api/admin/domains/${encodeURIComponent(d.domain)}`, body)),
    onSuccess: () => { setError(null); qc.invalidateQueries({ queryKey: ["domains"] }); },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });
  return (
    <div className="space-y-4 bg-bg/50 p-4 text-sm">
      <div className="grid gap-4 md:grid-cols-3">
        <div>
          <div className="text-xs text-muted">robots.txt</div>
          <div>{d.robots_status ?? "not fetched yet"}{d.robots_crawl_delay_s ? `, crawl-delay ${d.robots_crawl_delay_s}s` : ""}</div>
        </div>
        <div>
          <div className="text-xs text-muted">Egress groups</div>
          {d.egress_groups.map((g) => (
            <div key={g.egress_group}>
              {g.egress_group}: {g.rate_rps.toFixed(3)} req/s
              {g.cooldown_until && new Date(g.cooldown_until) > new Date() && <span className="text-warn"> · cooling down until {new Date(g.cooldown_until).toLocaleTimeString()}</span>}
              {g.latency_ewma_ms != null && <span className="text-muted"> · {Math.round(g.latency_ewma_ms)} ms</span>}
            </div>
          ))}
          {d.egress_groups.length === 0 && <div className="text-muted">none yet</div>}
        </div>
        {admin && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Input className="h-8 w-28" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="req/s cap" aria-label="Rate cap" />
              <Button size="sm" variant="outline" onClick={() =>
                update.mutate(rate ? { rate_rps: Number(rate) } : { clear_rate_override: true })}>Set cap</Button>
            </div>
            {d.block_status === "blocked" && (
              <Button size="sm" variant="outline" onClick={() => update.mutate({ unblock: true })}>Unblock</Button>
            )}
            <BlockOverrides d={d} save={(body) => update.mutate(body)} />
            {error && <div className="text-bad">{error}</div>}
          </div>
        )}
      </div>
      <div>
        <div className="mb-1 text-xs text-muted">Events</div>
        {events.data?.length === 0 ? <div className="text-muted">No events.</div> : (
          <ul className="space-y-0.5">
            {events.data?.map((e, i) => (
              <li key={i} className="text-xs">
                <span className="text-muted">{fmtAgo(e.at)}</span> · <b>{e.kind}</b> {e.reason}
                {e.old_rate_rps != null && ` · ${e.old_rate_rps.toFixed(3)} → ${e.new_rate_rps?.toFixed(3)} req/s`}
                {e.egress_group && <span className="text-muted"> ({e.egress_group})</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function DomainsPage() {
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const domains = useQuery({
    queryKey: ["domains", blockedOnly],
    queryFn: () => api.get<DomainRow[]>(`/api/admin/domains?limit=500${blockedOnly ? "&blocked=true" : ""}`),
    refetchInterval: 10_000,
  });
  return (
    <>
      <PageTitle title="Domains" subtitle="Rates, cooldowns, robots.txt and blocks per domain" />
      <Card>
        <CardHeader title="Domains" action={
          <label className="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={blockedOnly} onChange={(e) => setBlockedOnly(e.target.checked)} /> blocked only
          </label>} />
        {domains.data?.length === 0 ? <Empty>No domains yet. Queue URLs on the Jobs page or add a source.</Empty> : (
          <Table>
            <thead><tr><Th>Domain</Th><Th>Status</Th><Th>Rate</Th><Th className="text-right">Pages</Th><Th className="text-right">Queued</Th><Th>Last fetch</Th></tr></thead>
            <tbody>
              {domains.data?.map((d) => (
                <Fragment key={d.domain}>
                  <Tr onClick={() => setOpen(open === d.domain ? null : d.domain)} style={{ cursor: "pointer" }}>
                    <Td className="font-medium">{d.domain}</Td>
                    <Td>
                      {d.block_status === "blocked"
                        ? <Badge tone="bad">blocked: {d.block_reason}</Badge>
                        : d.egress_groups.some((g) => g.cooldown_until && new Date(g.cooldown_until) > new Date())
                          ? <Badge tone="warn">cooling down</Badge> : <Badge tone="ok">ok</Badge>}
                    </Td>
                    <Td className="tabular-nums text-muted">
                      {d.egress_groups.length ? `${Math.min(...d.egress_groups.map((g) => g.rate_rps)).toFixed(3)} req/s` : "—"}
                      {d.overrides.rate_rps != null && <span className="ml-1 text-xs">(cap {d.overrides.rate_rps})</span>}
                    </Td>
                    <Td className="text-right tabular-nums">{d.pages_fetched.toLocaleString()}</Td>
                    <Td className="text-right tabular-nums">{d.queued_jobs.toLocaleString()}</Td>
                    <Td className="text-muted">{fmtAgo(d.last_fetch_at)}</Td>
                  </Tr>
                  {open === d.domain && <tr><td colSpan={6} className="border-b border-line p-0"><Detail d={d} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
