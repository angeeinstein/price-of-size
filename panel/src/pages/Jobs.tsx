import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { useIsAdmin } from "@/lib/auth";
import { fmtAgo } from "@/lib/utils";

type JobRow = { id: string; type: string; status: string; attempts: number; max_attempts: number;
  domain: string | null; url: string | null; last_error: string | null; updated_at: string;
  not_before: string };

const STATUSES = ["", "queued", "leased", "failed", "done", "cancelled"];

function EnqueueUrls() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [source, setSource] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: () => api.post<{ queued: number; duplicates: number; invalid: string[] }>(
      "/api/admin/crawl/urls",
      { urls: text.split(/\s+/).filter(Boolean), source: source || null }),
    onSuccess: (r) => {
      setMsg(`${r.queued} queued, ${r.duplicates} already known, ${r.invalid.length} invalid`);
      setText("");
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e) => setMsg(e instanceof ApiError ? e.message : String(e)),
  });
  return (
    <Card>
      <CardHeader title="Queue URLs" subtitle="One per line; normalised and de-duplicated, fetched politely per domain" />
      <CardBody className="space-y-3">
        <textarea className="h-28 w-full rounded-md border border-line bg-panel p-2 font-mono text-xs outline-none focus:border-accent"
          value={text} onChange={(e) => setText(e.target.value)} placeholder="https://shop.example/p/123" aria-label="URLs" />
        <div className="flex items-end gap-3">
          <div className="w-64"><Label>Source</Label><Input value={source} onChange={(e) => setSource(e.target.value)} placeholder="e.g. ikea" /></div>
          <Button disabled={!text.trim() || m.isPending} onClick={() => m.mutate()}>Queue</Button>
          {msg && <span className="text-sm text-muted">{msg}</span>}
        </div>
      </CardBody>
    </Card>
  );
}

export function JobsPage() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const [status, setStatus] = useState("failed");
  const jobs = useQuery({
    queryKey: ["jobs", status],
    queryFn: () => api.get<JobRow[]>(`/api/admin/jobs?limit=200${status ? `&status=${status}` : ""}`),
    refetchInterval: 10_000,
  });
  const stats = useQuery({ queryKey: ["jobstats"], queryFn: () =>
    api.get<{ type: string; status: string; count: number }[]>("/api/admin/jobs/stats"), refetchInterval: 10_000 });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) => api.post(`/api/admin/jobs/${id}`, { action }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const types = [...new Set(stats.data?.map((s) => s.type) ?? [])].sort();
  const count = (t: string, s: string) => stats.data?.find((x) => x.type === t && x.status === s)?.count ?? 0;

  return (
    <>
      <PageTitle title="Jobs and queue" />
      <Card className="mb-6">
        <CardHeader title="Queue by type" />
        <Table>
          <thead><tr><Th>Type</Th>{["queued", "leased", "done", "failed"].map((s) => <Th key={s} className="text-right">{s}</Th>)}</tr></thead>
          <tbody>
            {types.map((t) => (
              <Tr key={t}><Td>{t}</Td>{["queued", "leased", "done", "failed"].map((s) =>
                <Td key={s} className="text-right tabular-nums">{count(t, s).toLocaleString()}</Td>)}</Tr>
            ))}
          </tbody>
        </Table>
      </Card>
      {admin && <div className="mb-6"><EnqueueUrls /></div>}
      <Card>
        <CardHeader title="Jobs" action={
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status filter"
            className="h-8 rounded-md border border-line bg-panel px-2 text-sm">
            {STATUSES.map((s) => <option key={s} value={s}>{s || "all"}</option>)}
          </select>} />
        {jobs.data?.length === 0 ? <Empty>No jobs with this status.</Empty> : (
          <Table>
            <thead><tr><Th>Updated</Th><Th>Type</Th><Th>Status</Th><Th>Target</Th><Th>Attempts</Th><Th>Last error</Th><Th /></tr></thead>
            <tbody>
              {jobs.data?.map((j) => (
                <Tr key={j.id}>
                  <Td className="whitespace-nowrap text-muted">{fmtAgo(j.updated_at)}</Td>
                  <Td>{j.type}</Td>
                  <Td><Badge tone={j.status === "failed" ? "bad" : j.status === "done" ? "ok" : "muted"}>{j.status}</Badge></Td>
                  <Td className="max-w-xs truncate" title={j.url ?? ""}>{j.url ?? j.domain ?? "—"}</Td>
                  <Td className="tabular-nums">{j.attempts}/{j.max_attempts}</Td>
                  <Td className="max-w-sm"><pre className="max-h-20 overflow-hidden whitespace-pre-wrap text-xs text-muted">{j.last_error}</pre></Td>
                  <Td className="whitespace-nowrap">
                    {admin && (j.status === "failed" || j.status === "cancelled") &&
                      <Button size="sm" variant="outline" onClick={() => act.mutate({ id: j.id, action: "retry" })}>Retry</Button>}
                    {admin && j.status === "queued" &&
                      <Button size="sm" variant="ghost" onClick={() => act.mutate({ id: j.id, action: "cancel" })}>Cancel</Button>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
