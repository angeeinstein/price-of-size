import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Empty } from "@/components/ui/table";
import { api, ApiError, type SourceRow } from "@/lib/api";
import { useIsAdmin } from "@/lib/auth";

const EXAMPLE = `{
  "domains": ["www.example-shop.at"],
  "product_url_pattern": "/p/[^/]+-\\\\d+/?$",
  "sitemaps": [],
  "seeds": [],
  "priority": 0
}`;

function SourceEditor({ source, onDone }: { source?: SourceRow; onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(source?.name ?? "");
  const [config, setConfig] = useState(source ? JSON.stringify(source.config, null, 2) : EXAMPLE);
  const [status, setStatus] = useState(source?.status ?? "active");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      let parsed: unknown;
      try { parsed = JSON.parse(config); } catch { throw new Error("config is not valid JSON"); }
      return api.put(`/api/admin/sources/${encodeURIComponent(name)}`, { type: "scraper", status, config: parsed as Record<string, unknown> });
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["sources"] }); onDone(); },
    onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : String(e)),
  });
  return (
    <CardBody className="space-y-3 border-b border-line">
      <div className="flex gap-3">
        <div className="w-64"><Label>Name</Label><Input value={name} disabled={!!source} onChange={(e) => setName(e.target.value)} /></div>
        <div><Label>Status</Label>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="h-9 rounded-md border border-line bg-panel px-2 text-sm">
            {["active", "paused", "disabled"].map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
      </div>
      <div><Label>Config (JSON)</Label>
        <textarea className="h-48 w-full rounded-md border border-line bg-panel p-2 font-mono text-xs outline-none focus:border-accent"
          value={config} onChange={(e) => setConfig(e.target.value)} aria-label="Source config" /></div>
      {error && <p className="text-sm text-bad">{error}</p>}
      <div className="flex gap-2">
        <Button onClick={() => save.mutate()} disabled={!name}>Save</Button>
        <Button variant="outline" onClick={onDone}>Cancel</Button>
      </div>
    </CardBody>
  );
}

export function SourcesPage() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const sources = useQuery({ queryKey: ["sources"], queryFn: () => api.get<SourceRow[]>("/api/admin/sources") });
  const [editing, setEditing] = useState<string | null>(null);
  const [msg, setMsg] = useState<Record<string, string>>({});
  const discover = useMutation({
    mutationFn: (name: string) => api.post<{ sitemaps: number; seeds: number }>(`/api/admin/sources/${encodeURIComponent(name)}/discover`),
    onSuccess: (r, name) => { setMsg({ ...msg, [name]: `queued ${r.sitemaps} sitemaps, ${r.seeds} seeds` });
      qc.invalidateQueries({ queryKey: ["jobs"] }); },
    onError: (e, name) => setMsg({ ...msg, [name]: e instanceof ApiError ? e.message : String(e) }),
  });
  return (
    <>
      <PageTitle title="Sources" subtitle="Where URLs come from: sitemaps, seeds and which pages count as products"
        action={admin && <Button onClick={() => setEditing("__new__")}>New source</Button>} />
      <Card>
        {editing === "__new__" && <SourceEditor onDone={() => setEditing(null)} />}
        {sources.data?.length === 0 && editing !== "__new__" ? <Empty>No sources yet.</Empty> : null}
        {sources.data?.map((s) => (
          <div key={s.name} className="border-b border-line last:border-0">
            {editing === s.name ? <SourceEditor source={s} onDone={() => setEditing(null)} /> : (
              <div className="flex items-start justify-between gap-4 px-5 py-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 font-medium">{s.name}
                    <Badge tone={s.status === "active" ? "ok" : "muted"}>{s.status}</Badge></div>
                  <pre className="mt-2 max-h-32 overflow-auto text-xs text-muted">{JSON.stringify(s.config, null, 2)}</pre>
                  {msg[s.name] && <div className="mt-1 text-sm text-muted">{msg[s.name]}</div>}
                </div>
                {admin && (
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" variant="outline" onClick={() => setEditing(s.name)}>Edit</Button>
                    <Button size="sm" disabled={s.status !== "active"} onClick={() => discover.mutate(s.name)}>Discover</Button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </Card>
    </>
  );
}
