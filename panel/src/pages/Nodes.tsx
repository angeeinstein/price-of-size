import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError, type NodeRow, type ReleaseRow } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";
import { fmtAgo, fmtBytes } from "@/lib/utils";

type Command = { id: string; type: string; created_at: string; acked_at: string | null;
  ok: boolean | null; result: { output?: unknown } | null };

function stateBadge(n: NodeRow) {
  if (n.revoked_at) return <Badge tone="bad">revoked</Badge>;
  if (!n.online) return <Badge tone="warn">offline</Badge>;
  const s = n.actual_state ?? "?";
  return <Badge tone={s === "running" ? "ok" : "warn"}>{s}</Badge>;
}

function NodeDetail({ node, releases }: { node: NodeRow; releases: ReleaseRow[] }) {
  const qc = useQueryClient();
  const { sensitive } = useAuth();
  const admin = useIsAdmin();
  const [error, setError] = useState<string | null>(null);
  const commands = useQuery({
    queryKey: ["commands", node.id],
    queryFn: () => api.get<Command[]>(`/api/admin/nodes/${node.id}/commands`),
    refetchInterval: 5_000,
  });
  const act = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => sensitive(fn),
    onSuccess: () => { setError(null); qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["commands", node.id] }); },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });
  const patch = (body: Record<string, unknown>) =>
    act.mutate(() => api.patch(`/api/admin/nodes/${node.id}`, body));
  const command = (type: string) =>
    act.mutate(() => api.post(`/api/admin/nodes/${node.id}/commands`, { type }));
  const a = node.agent_status;

  return (
    <div className="space-y-4 bg-bg/50 px-4 py-4">
      <div className="grid gap-4 text-sm md:grid-cols-3">
        <div>
          <div className="text-xs text-muted">Resources</div>
          <div>{node.resources.cpu_limit?.toFixed(1) ?? "?"} cores, load {node.resources.cpu_load?.toFixed(2) ?? "?"}</div>
          <div>{fmtBytes(node.resources.mem_used_bytes)} / {fmtBytes(node.resources.mem_limit_bytes)} RAM</div>
          <div>{node.concurrency.http_slots ?? 0} HTTP, {node.concurrency.browser_slots ?? 0} browser slots</div>
        </div>
        <div>
          <div className="text-xs text-muted">Agent</div>
          <div>running {a?.running_version ?? "—"} · agent {a?.agent_version ?? "—"}</div>
          <div>update: {a?.update_state ?? "—"}</div>
          {a?.update_error && <div className="text-bad">{a.update_error}</div>}
        </div>
        <div>
          <div className="text-xs text-muted">Network</div>
          <div>egress {node.egress_ip ?? "—"}</div>
          <div>group {node.egress_group ?? "—"}</div>
        </div>
      </div>
      {admin && !node.revoked_at && (
        <div className="flex flex-wrap items-center gap-2">
          {node.desired_run_state !== "running" && <Button size="sm" onClick={() => patch({ run_state: "running" })}>Resume</Button>}
          {node.desired_run_state === "running" && <Button size="sm" variant="outline" onClick={() => patch({ run_state: "paused" })}>Pause</Button>}
          <Button size="sm" variant="outline" onClick={() => patch({ run_state: "draining" })}>Drain</Button>
          <select className="h-8 rounded-md border border-line bg-panel px-2 text-sm" value={node.desired_target_version ?? ""}
            onChange={(e) => patch({ target_version: e.target.value || null })} aria-label="Target version">
            <option value="">no target version</option>
            {releases.map((r) => <option key={r.manifest.version} value={r.manifest.version}>run {r.manifest.version}</option>)}
          </select>
          <Button size="sm" variant="outline" onClick={() => command("diagnostics")}>Diagnostics</Button>
          <Button size="sm" variant="outline" onClick={() => command("fetch_logs")}>Fetch logs</Button>
          <Button size="sm" variant="outline" onClick={() => command("restart")}>Restart</Button>
          <Button size="sm" variant="danger" onClick={() => {
            if (confirm(`Revoke ${node.name}? It loses access immediately.`))
              act.mutate(() => api.post(`/api/admin/nodes/${node.id}/revoke`));
          }}>Revoke</Button>
        </div>
      )}
      {error && <p className="text-sm text-bad">{error}</p>}
      {(commands.data?.length ?? 0) > 0 && (
        <div>
          <div className="mb-1 text-xs text-muted">Recent commands</div>
          <div className="space-y-2">
            {commands.data!.slice(0, 5).map((c) => (
              <details key={c.id} className="rounded-md border border-line bg-panel p-2 text-sm">
                <summary className="cursor-pointer">
                  {c.type} · {fmtAgo(c.created_at)} · {c.acked_at ? (c.ok ? "ok" : "failed") : "pending"}
                </summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs">
                  {JSON.stringify(c.result?.output ?? null, null, 2)}
                </pre>
              </details>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EnrollmentCode() {
  const { sensitive } = useAuth();
  const [code, setCode] = useState<{ code: string; expires_at: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function create() {
    try {
      setCode(await sensitive(() => api.post("/api/admin/enrollment-codes", { label: "panel" })));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }
  return (
    <div className="flex items-center gap-3">
      {code && <code className="rounded bg-line px-2 py-1 font-mono text-sm">{code.code}</code>}
      {error && <span className="text-sm text-bad">{error}</span>}
      <Button onClick={create}>Add node</Button>
    </div>
  );
}

export function NodesPage() {
  const admin = useIsAdmin();
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: () => api.get<NodeRow[]>("/api/admin/nodes"),
    refetchInterval: 5_000 });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => api.get<ReleaseRow[]>("/api/admin/releases") });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <PageTitle title="Nodes" subtitle="Every machine that collects data" action={admin && <EnrollmentCode />} />
      <Card>
        <CardHeader title="Nodes" subtitle="Click a node for details and controls" />
        {nodes.data?.length === 0 ? (
          <CardBody><Empty>No nodes yet. Add one with an enrollment code: <code>install.sh --role node --enroll CODE</code></Empty></CardBody>
        ) : (
          <Table>
            <thead><tr><Th>Name</Th><Th>State</Th><Th>Version</Th><Th>Jobs</Th><Th>Egress group</Th><Th>Last heartbeat</Th></tr></thead>
            <tbody>
              {nodes.data?.map((n) => (
                <Fragment key={n.id}>
                  <Tr onClick={() => setOpen(open === n.id ? null : n.id)} style={{ cursor: "pointer" }}>
                    <Td className="font-medium">{n.name}<div className="text-xs text-muted">{n.trust_level}</div></Td>
                    <Td>{stateBadge(n)}{n.desired_run_state !== "running" && <span className="ml-1 text-xs text-muted">→ {n.desired_run_state}</span>}</Td>
                    <Td>{n.version ?? "—"}{n.desired_target_version && n.desired_target_version !== n.version &&
                      <span className="ml-1 text-xs text-warn">→ {n.desired_target_version}</span>}</Td>
                    <Td className="tabular-nums">{n.concurrency.active_jobs ?? 0}{(n.concurrency.spooled_results ?? 0) > 0 &&
                      <span className="text-xs text-muted"> ({n.concurrency.spooled_results} spooled)</span>}</Td>
                    <Td className="text-muted">{n.egress_group ?? "—"}</Td>
                    <Td className="text-muted">{fmtAgo(n.last_heartbeat_at)}</Td>
                  </Tr>
                  {open === n.id && (
                    <tr><td colSpan={6} className="border-b border-line p-0">
                      <NodeDetail node={n} releases={releases.data ?? []} />
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
