import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError, type ReleaseRow } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";
import { fmtTime } from "@/lib/utils";

// Releases are registered from the GitHub release's release.json; the coordinator signs
// them, and nodes only ever run releases signed by their coordinator.
export function ReleasesPage() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const { sensitive } = useAuth();
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => api.get<ReleaseRow[]>("/api/admin/releases") });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Record<string, unknown>>("/api/admin/settings") });
  const key = useQuery({ queryKey: ["signing-key"], queryFn: () => api.get<{ key_id: string }>("/api/admin/releases/signing-key") });
  const [json, setJson] = useState("");
  const [error, setError] = useState<string | null>(null);

  const register = useMutation({
    mutationFn: async () => {
      let body: Record<string, unknown>;
      try { body = JSON.parse(json); } catch { throw new Error("not valid JSON"); }
      return sensitive(() => api.post("/api/admin/releases", { version: body.version, images: body.images, notes: body.notes ?? null }));
    },
    onSuccess: () => { setJson(""); setError(null); qc.invalidateQueries({ queryKey: ["releases"] }); },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });
  const coordinatorTarget = useMutation({
    mutationFn: (version: string | null) =>
      sensitive(() => api.put("/api/admin/settings/coordinator.target_version", { value: version })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });
  const target = settings.data?.["coordinator.target_version"] as string | null | undefined;

  return (
    <>
      <PageTitle title="Releases" subtitle={`Signed by this coordinator (key ${key.data?.key_id ?? "…"})`} />
      {admin && (
        <Card className="mb-6">
          <CardHeader title="Register a release" subtitle="Paste release.json from the GitHub release (images pinned by digest)" />
          <CardBody className="space-y-3">
            <textarea className="h-32 w-full rounded-md border border-line bg-panel p-2 font-mono text-xs outline-none focus:border-accent"
              value={json} onChange={(e) => setJson(e.target.value)} placeholder='{"version": "0.2.0", "images": {...}}' aria-label="release.json" />
            {error && <p className="text-sm text-bad">{error}</p>}
            <Button disabled={!json.trim()} onClick={() => register.mutate()}>Register and sign</Button>
          </CardBody>
        </Card>
      )}
      <Card>
        <CardHeader title="Releases" subtitle="Roll out to one node first (Nodes page), then the rest" />
        {releases.data?.length === 0 ? <Empty>No releases registered yet.</Empty> : (
          <Table>
            <thead><tr><Th>Version</Th><Th>Images</Th><Th>Registered</Th><Th>Coordinator</Th></tr></thead>
            <tbody>
              {releases.data?.map((r) => (
                <Tr key={r.manifest.version}>
                  <Td className="font-medium">{r.manifest.version}</Td>
                  <Td>{Object.entries(r.manifest.images).map(([c, ref]) => (
                    <div key={c} className="text-xs"><span className="text-muted">{c}</span> <code>{ref.split("@")[1]?.slice(0, 19)}…</code></div>
                  ))}</Td>
                  <Td className="text-muted">{fmtTime(r.manifest.created_at)}</Td>
                  <Td>
                    {target === r.manifest.version ? <Badge tone="accent">target</Badge> : admin && (
                      <Button size="sm" variant="outline" onClick={() => {
                        if (confirm(`Update the coordinator to ${r.manifest.version}? (backup, migrations, blue-green switch)`))
                          coordinatorTarget.mutate(r.manifest.version);
                      }}>Run on coordinator</Button>
                    )}
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
