import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/card";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type Overview } from "@/lib/api";
import { fmtAgo } from "@/lib/utils";

// Live: server-sent events push a fresh overview every few seconds; the query is the
// first paint and the fallback if the stream is unavailable.
function useOverview() {
  const q = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.get<Overview>("/api/panel/overview"),
    refetchInterval: 15_000,
  });
  const [live, setLive] = useState<Overview | null>(null);
  useEffect(() => {
    const es = new EventSource("/api/panel/stream");
    es.addEventListener("overview", (e) => setLive(JSON.parse((e as MessageEvent).data)));
    return () => es.close();
  }, []);
  return live ?? q.data ?? null;
}

function HourBars({ data }: { data: Overview["fetches_per_hour"] }) {
  if (!data.some((d) => d.fetches > 0)) return <Empty>No fetches in the last 24 hours.</Empty>;
  const max = Math.max(...data.map((d) => d.fetches), 1);
  const hour = (h: string) => new Date(h).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return (
    <div>
      <div className="flex h-32 items-end gap-1" aria-label="Fetches per hour">
        {data.map((d) => (
          <div
            key={d.hour}
            className="flex-1 rounded-t bg-accent/70"
            style={{ height: `${(d.fetches / max) * 100}%` }}
            title={`${new Date(d.hour).toLocaleString()}: ${d.fetches}`}
          />
        ))}
      </div>
      <div className="mt-1 flex justify-between text-xs text-muted">
        <span>{hour(data[0].hour)}</span>
        <span>peak {max.toLocaleString()} / h</span>
        <span>{hour(data[data.length - 1].hour)}</span>
      </div>
    </div>
  );
}

export function OverviewPage() {
  const o = useOverview();
  if (!o) return <div className="text-muted">Loading…</div>;
  const byStatus = (s: string) =>
    o.jobs.filter((j) => j.status === s).reduce((a, j) => a + j.count, 0);
  const budgetTone =
    o.budget.state === "exhausted" ? "bad" : o.budget.state === "warn" ? "warn" : "ok";
  return (
    <>
      <PageTitle title="Overview" subtitle={`updated ${fmtAgo(o.at)}`} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Nodes online" value={`${o.nodes.online} / ${o.nodes.total}`} tone={o.nodes.online ? "ok" : "warn"} />
        <Stat label="Fetches (24 h)" value={o.fetches_24h.toLocaleString()} />
        <Stat label="Jobs queued" value={byStatus("queued").toLocaleString()} hint={`${byStatus("leased")} running`} />
        <Stat label="Domains" value={o.domains.total.toLocaleString()} hint={`${o.domains.blocked} blocked`}
          tone={o.domains.blocked ? "warn" : undefined} />
        <Stat label="LLM budget" value={`$${Number(o.budget.spent_month_usd).toFixed(2)}`}
          hint={`of $${Number(o.budget.hard_cap_usd).toFixed(2)} this month`} tone={budgetTone} />
      </div>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Fetches per hour" subtitle="last 24 hours" />
          <CardBody><HourBars data={o.fetches_per_hour} /></CardBody>
        </Card>
        <Card>
          <CardHeader title="Recent domain events" />
          {o.recent_domain_events.length === 0 ? (
            <Empty>Nothing happened yet.</Empty>
          ) : (
            <Table>
              <tbody>
                {o.recent_domain_events.map((e, i) => (
                  <Tr key={i}>
                    <Td className="whitespace-nowrap text-muted">{fmtAgo(e.at)}</Td>
                    <Td>{e.domain}</Td>
                    <Td>
                      <Badge tone={e.kind === "blocked" ? "bad" : e.kind === "rate_down" ? "warn" : "muted"}>{e.kind}</Badge>
                    </Td>
                    <Td className="text-muted">{e.reason}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
      <Card className="mt-6">
        <CardHeader title="Jobs by type" />
        {o.jobs.length === 0 ? (
          <Empty>No jobs yet.</Empty>
        ) : (
          <Table>
            <thead><tr><Th>Type</Th><Th>Status</Th><Th className="text-right">Count</Th></tr></thead>
            <tbody>
              {o.jobs.map((j) => (
                <Tr key={`${j.type}-${j.status}`}>
                  <Td>{j.type}</Td>
                  <Td><Badge tone={j.status === "failed" ? "bad" : j.status === "done" ? "ok" : "muted"}>{j.status}</Badge></Td>
                  <Td className="text-right tabular-nums">{j.count.toLocaleString()}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
