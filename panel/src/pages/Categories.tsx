import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError, type CategoryPathRow, type TaxonomyHit } from "@/lib/api";
import { useIsAdmin } from "@/lib/auth";

function Mapper({ row, onDone }: { row: CategoryPathRow; onDone: () => void }) {
  const qc = useQueryClient();
  // start with the shop's own leaf name: often a good search
  const [q, setQ] = useState(row.path.split(" > ").pop() ?? "");
  const [locale, setLocale] = useState("de");
  const [error, setError] = useState<string | null>(null);
  const hits = useQuery({
    queryKey: ["taxonomy-search", q, locale],
    queryFn: () => api.get<TaxonomyHit[]>(
      `/api/admin/taxonomy/search?q=${encodeURIComponent(q)}&locale=${locale}&limit=40`),
    enabled: q.trim().length > 1,
  });
  const save = useMutation({
    mutationFn: (node_id: string | null) =>
      api.put("/api/admin/category-mappings", { source: row.source, path: row.path, node_id }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["category-paths"] }); onDone(); },
    onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
  });
  return (
    <div className="space-y-2 bg-bg/50 p-3 text-sm">
      <div className="flex items-center gap-2">
        <Input className="h-8 w-80" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the taxonomy" />
        <select value={locale} onChange={(e) => setLocale(e.target.value)} aria-label="Language"
          className="h-8 rounded-md border border-line bg-panel px-2 text-xs">
          <option value="de">Deutsch</option><option value="en">English</option>
        </select>
        <Button size="sm" variant="outline" onClick={() => save.mutate(null)}>No fitting category</Button>
        <Button size="sm" variant="outline" onClick={onDone}>Close</Button>
      </div>
      {error && <div className="text-bad">{error}</div>}
      <ul className="max-h-72 space-y-0.5 overflow-auto">
        {hits.data?.map((h) => (
          <li key={h.id}>
            <button className="text-left hover:text-accent" onClick={() => save.mutate(h.id)}>
              {(locale !== "en" && h.local_path) || h.path}
              <span className="ml-2 text-xs text-muted">{h.external_id}</span>
              {h.is_custom && <> <Badge tone="muted">extension</Badge></>}
            </button>
          </li>
        ))}
        {hits.data?.length === 0 && <li className="text-muted">No category matches all words.</li>}
      </ul>
    </div>
  );
}

export function CategoriesPage() {
  const admin = useIsAdmin();
  const [unmapped, setUnmapped] = useState(true);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const taxonomy = useQuery({ queryKey: ["taxonomy"], queryFn: () =>
    api.get<{ nodes: number; versions: { version: string; custom: boolean; nodes: number }[] }>("/api/admin/taxonomy") });
  const paths = useQuery({
    queryKey: ["category-paths", unmapped, q],
    queryFn: () => api.get<CategoryPathRow[]>(
      `/api/admin/category-paths?unmapped=${unmapped}&limit=300${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Categories" />
      <p className="text-sm text-muted">
        Shop categories (breadcrumbs) mapped once to the target taxonomy; every product under a mapped path
        is classified by it. Most frequent paths first.
        {taxonomy.data && taxonomy.data.nodes === 0 &&
          " The taxonomy is empty: import it with pos-coordinator taxonomy-import (or POST /api/admin/taxonomy/import)."}
      </p>
      <Card>
        <CardHeader
          title="Paths"
          subtitle={taxonomy.data ? `taxonomy: ${taxonomy.data.nodes} categories` : undefined}
          action={
            <div className="flex items-center gap-3 text-sm">
              <Input className="h-8 w-56" placeholder="filter paths" value={q} onChange={(e) => setQ(e.target.value)} />
              <label className="flex items-center gap-1">
                <input type="checkbox" checked={unmapped} onChange={(e) => setUnmapped(e.target.checked)} /> unmapped only
              </label>
            </div>
          }
        />
        {paths.data?.length === 0 ? <Empty>No category paths{unmapped ? " left to map" : ""}.</Empty> : (
          <Table>
            <thead><tr><Th>Source</Th><Th>Shop category</Th><Th className="text-right">Products</Th><Th>Mapped to</Th><Th /></tr></thead>
            <tbody>
              {paths.data?.map((r) => {
                const key = `${r.source}\u0000${r.path}`;
                return (
                  <Fragment key={key}>
                    <Tr>
                      <Td>{r.source}</Td>
                      <Td>{r.path}</Td>
                      <Td className="text-right tabular-nums">{r.products}</Td>
                      <Td>{r.mapping_id ? (r.node_path ?? <span className="text-muted">no fitting category</span>) : <span className="text-muted">—</span>}
                        {r.method && r.method !== "manual" && <> <Badge tone="muted">{r.method}</Badge></>}</Td>
                      <Td>{admin && <Button size="sm" variant="outline" onClick={() => setOpen(open === key ? null : key)}>Map</Button>}</Td>
                    </Tr>
                    {open === key && (
                      <tr><td colSpan={5}><Mapper row={r} onDone={() => setOpen(null)} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
