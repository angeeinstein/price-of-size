import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import {
  api, ApiError, type CategoryPathRow, type ClassificationStatus, type KnnEval, type TaxonomyHit,
} from "@/lib/api";
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

const TIER_LABEL: Record<string, string> = {
  manual: "manual", llm: "LLM pick", shop_mapping: "shop mapping", knn: "k-NN",
};

function Classification() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const [showEval, setShowEval] = useState(false);
  const status = useQuery({ queryKey: ["classification"], refetchInterval: 30_000,
    queryFn: () => api.get<ClassificationStatus>("/api/admin/classification") });
  const evaluation = useQuery({ queryKey: ["knn-eval"], enabled: showEval,
    queryFn: () => api.get<KnnEval>("/api/admin/classification/knn-eval") });
  const rerun = useMutation({
    mutationFn: () => api.post<{ generation: number }>("/api/admin/classification/rerun"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["classification"] }),
  });
  const s = status.data;
  if (!s) return null;
  const share = (n: number) => (s.products ? `${Math.round((100 * n) / s.products)} %` : "—");
  return (
    <Card>
      <CardHeader
        title="Classification"
        subtitle={`${s.products} products, ${s.products - s.unclassified} classified (${share(s.products - s.unclassified)}), ${s.embedded} embedded; generation ${s.generation}`}
        action={
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setShowEval(!showEval)}>
              {showEval ? "Hide" : "Check"} k-NN
            </Button>
            {admin && (
              <Button size="sm" variant="outline" disabled={rerun.isPending}
                onClick={() => {
                  if (window.confirm("Run every classifier again over the stored data? Unchanged LLM questions are answered from the ledger; new ones cost budget."))
                    rerun.mutate();
                }}>
                Run again
              </Button>
            )}
          </div>
        }
      />
      <Table>
        <thead><tr><Th>Tier</Th><Th>Version</Th><Th className="text-right">Products</Th>
          <Th className="text-right">Mean confidence</Th><Th className="text-right">&lt; 0.6</Th><Th className="text-right">≥ 0.8</Th></tr></thead>
        <tbody>
          {s.current.length === 0 && (
            <Tr><Td className="text-muted" colSpan={6}>No product is classified yet.</Td></Tr>
          )}
          {s.current.map((r) => (
            <Tr key={`${r.tier}/${r.version}`}>
              <Td>{TIER_LABEL[r.tier] ?? r.tier}</Td>
              <Td className="font-mono text-xs">{r.version}</Td>
              <Td className="text-right tabular-nums">{r.products}</Td>
              <Td className="text-right tabular-nums">{r.avg_confidence ?? "—"}</Td>
              <Td className="text-right tabular-nums">{r.below_0_6}</Td>
              <Td className="text-right tabular-nums">{r.at_least_0_8}</Td>
            </Tr>
          ))}
        </tbody>
      </Table>
      <div className="space-y-1 p-3 text-sm text-muted">
        {s.attempts.length > 0 && (
          <div>Kept as attempts (not current): {s.attempts.map((a) =>
            `${TIER_LABEL[a.tier] ?? a.tier} ${a.rows} (${a.no_category} without a category)`).join(", ")}</div>
        )}
        {s.agreement.length > 0 && (
          <div>LLM labels agree with the earlier classification: {s.agreement.map((a) =>
            `${TIER_LABEL[a.tier] ?? a.tier} ${a.agreed} of ${a.compared} (${Math.round((100 * a.agreed) / a.compared)} %)`).join(", ")}</div>
        )}
        {s.batches.length > 0 && (
          <div>Waiting for LLM batches: {s.batches.map((b) => `${b.stage} ${b.requests} in ${b.batches}`).join(", ")}</div>
        )}
      </div>
      {showEval && evaluation.data && (
        <div className="border-t border-line p-3 text-sm">
          <p className="mb-2 text-muted">
            Leave-one-out on {evaluation.data.evaluated} LLM and manual labels ({evaluation.data.examples} examples):
            what share the k-NN classifier keeps at each threshold, and how often it agrees.
          </p>
          <Table>
            <thead><tr><Th>Threshold</Th><Th className="text-right">Coverage</Th><Th className="text-right">Accuracy</Th></tr></thead>
            <tbody>
              {evaluation.data.thresholds.map((t) => (
                <Tr key={t.threshold}>
                  <Td className="tabular-nums">{t.threshold}</Td>
                  <Td className="text-right tabular-nums">{Math.round(t.coverage * 100)} %</Td>
                  <Td className="text-right tabular-nums">{t.accuracy === null ? "—" : `${Math.round(t.accuracy * 100)} %`}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

type View = "unmapped" | "llm" | "all";

export function CategoriesPage() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const [view, setView] = useState<View>("unmapped");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const taxonomy = useQuery({ queryKey: ["taxonomy"], queryFn: () =>
    api.get<{ nodes: number; versions: { version: string; custom: boolean; nodes: number }[] }>("/api/admin/taxonomy") });
  const paths = useQuery({
    queryKey: ["category-paths", view, q],
    queryFn: () => api.get<CategoryPathRow[]>(
      `/api/admin/category-paths?unmapped=${view === "unmapped"}${view === "llm" ? "&method=llm" : ""}` +
      `&limit=300${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  });
  // confirming an LLM proposal makes it a manual mapping to the same category
  const confirm = useMutation({
    mutationFn: (r: CategoryPathRow) =>
      api.put("/api/admin/category-mappings", { source: r.source, path: r.path, node_id: r.taxonomy_node_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["category-paths"] }),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Categories" />
      <p className="text-sm text-muted">
        Shop categories (breadcrumbs) mapped once to the target taxonomy; every product under a mapped path
        is classified by it. Most frequent paths first. Paths the LLM mapped are marked; check them under
        “LLM proposals” and confirm or change them.
        {taxonomy.data && taxonomy.data.nodes === 0 &&
          " The taxonomy is empty: import it with pos-coordinator taxonomy-import (or POST /api/admin/taxonomy/import)."}
      </p>
      <Classification />
      <Card>
        <CardHeader
          title="Paths"
          subtitle={taxonomy.data ? `taxonomy: ${taxonomy.data.nodes} categories` : undefined}
          action={
            <div className="flex items-center gap-3 text-sm">
              <Input className="h-8 w-56" placeholder="filter paths" value={q} onChange={(e) => setQ(e.target.value)} />
              <select value={view} onChange={(e) => setView(e.target.value as View)} aria-label="Show"
                className="h-8 rounded-md border border-line bg-panel px-2 text-xs">
                <option value="unmapped">Unmapped</option>
                <option value="llm">LLM proposals</option>
                <option value="all">All</option>
              </select>
            </div>
          }
        />
        {paths.data?.length === 0 ? <Empty>No category paths{view === "unmapped" ? " left to map" : view === "llm" ? " mapped by the LLM" : ""}.</Empty> : (
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
                        {r.method && r.method !== "manual" && <> <Badge tone="muted">{r.method}{r.confidence !== null && r.method === "llm" ? ` ${r.confidence}` : ""}</Badge></>}
                        {r.method === "llm" && r.note && <div className="text-xs text-muted">{r.note}</div>}</Td>
                      <Td className="whitespace-nowrap">{admin && <>
                        {r.method === "llm" && (
                          <Button size="sm" variant="outline" className="mr-1" disabled={confirm.isPending}
                            onClick={() => confirm.mutate(r)}>Confirm</Button>
                        )}
                        <Button size="sm" variant="outline" onClick={() => setOpen(open === key ? null : key)}>Map</Button>
                      </>}</Td>
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
