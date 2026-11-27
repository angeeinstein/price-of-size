import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type ParserRow, type ParserTestRun } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";

function TestRun({ id }: { id: string }) {
  const runs = useQuery({ queryKey: ["parser-test", id],
    queryFn: () => api.get<ParserTestRun[]>(`/api/admin/parsers/${id}/test?limit=10`) });
  const detail = useQuery({ queryKey: ["parser", id],
    queryFn: () => api.get<{ artifact: unknown }>(`/api/admin/parsers/${id}`) });
  return (
    <div className="space-y-3 bg-bg/50 p-3 text-sm">
      {runs.isError && <div className="text-bad">{String(runs.error)}</div>}
      {runs.data?.length === 0 && <div className="text-muted">No archived pages of this domain.</div>}
      {runs.data?.map((r) => (
        <div key={r.url} className="rounded-md border border-line p-2">
          <div className="flex items-center gap-2">
            <a className="truncate hover:text-accent" href={r.url} target="_blank" rel="noreferrer noopener">{r.url}</a>
            {r.passes === true && <Badge tone="ok">matches the LLM</Badge>}
            {r.passes === false && <Badge tone="bad">differs from the LLM</Badge>}
          </div>
          <div className="mt-1 grid grid-cols-2 gap-3 text-xs">
            <div>
              <div className="text-muted">Parser</div>
              <div>{r.found.title}</div>
              <div>price: {r.found.price ?? "—"} {r.found.currency ?? ""}</div>
              {(r.found.availability || r.found.condition) && (
                <div>{[r.found.availability, r.found.condition].filter(Boolean).join(", ")}</div>
              )}
              {r.found.sizes.map((s, i) => <div key={i}>{s.label}: {s.value}</div>)}
            </div>
            <div>
              <div className="text-muted">LLM tier</div>
              {r.llm ? <>
                <div>price: {r.llm.price_text ?? "—"} {r.llm.currency ?? ""}
                  {r.llm.price_kind && !["item", "unknown"].includes(r.llm.price_kind) && <> ({r.llm.price_kind})</>}</div>
                {[r.llm.availability, r.llm.condition].some((v) => v && v !== "unknown") && (
                  <div>{[r.llm.availability, r.llm.condition].filter((v) => v && v !== "unknown").join(", ")}</div>
                )}
                {r.llm.sizes?.map((s, i) => <div key={i}>{s.label}: {s.value}</div>)}
              </> : <div className="text-muted">not read by the LLM</div>}
            </div>
          </div>
        </div>
      ))}
      {detail.data && (
        <details>
          <summary className="cursor-pointer text-muted">Spec</summary>
          <pre className="mt-2 overflow-auto text-xs">{JSON.stringify(detail.data.artifact, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

export function ParsersPage() {
  const admin = useIsAdmin();
  const { sensitive } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const parsers = useQuery({ queryKey: ["parsers"], queryFn: () => api.get<ParserRow[]>("/api/admin/parsers") });
  const toggle = useMutation({
    // (de)activating a parser asks for a recent sign-in
    mutationFn: (p: ParserRow) =>
      sensitive(() => api.post(`/api/admin/parsers/${p.id}/${p.active ? "deactivate" : "activate"}`)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["parsers"] }),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Parsers" />
      <p className="text-sm text-muted">
        Per-site parsers: CSS selectors the LLM wrote for a shop from pages the LLM tier had read, checked on
        further pages before they run. A page passes when the parser finds every price and size the LLM found
        there and nothing else, with the same currency, availability and condition and the same kind of size (product or package,
        net or gross). One version
        per domain is active; every version is kept.
      </p>
      <Card>
        <CardHeader title="Versions" />
        {parsers.data?.length === 0 ? <Empty>No parsers yet: they appear once the LLM tier has read enough pages of a shop.</Empty> : (
          <Table>
            <thead><tr><Th>Domain</Th><Th>Version</Th><Th className="text-right">Held-out pages</Th><Th>State</Th><Th>Made by</Th><Th /></tr></thead>
            <tbody>
              {parsers.data?.map((p) => (
                <Fragment key={p.id}>
                  <Tr>
                    <Td>{p.domain}</Td>
                    <Td className="tabular-nums">{p.version}</Td>
                    <Td className="text-right tabular-nums">
                      {p.held_out ? `${p.held_out[0]} of ${p.held_out[1]}` : "—"}
                      {p.validation_score !== null && !p.error && <span className="ml-1 text-xs text-muted">({Math.round(p.validation_score * 100)} %)</span>}
                    </Td>
                    <Td>{p.active ? <Badge tone="ok">active</Badge> : p.error ? <Badge tone="bad">refused</Badge> : <Badge tone="muted">inactive</Badge>}
                      {p.error && <div className="text-xs text-muted">{p.error}</div>}
                      {!p.active && !p.error && p.qualified_prices > 0 && (
                        <div className="text-xs text-muted">
                          {p.qualified_prices} checked {p.qualified_prices === 1 ? "page shows" : "pages show"} a unit, monthly,
                          net or “from” price, which a parser reads as the item price: activate by hand after a test run
                        </div>
                      )}</Td>
                    <Td className="text-xs text-muted">{p.created_by} · {new Date(p.created_at).toLocaleString()}</Td>
                    <Td className="whitespace-nowrap">
                      {!p.error && <Button size="sm" variant="outline" className="mr-1" onClick={() => setOpen(open === p.id ? null : p.id)}>Test</Button>}
                      {admin && !p.error && (
                        <Button size="sm" variant="outline" disabled={toggle.isPending} onClick={() => toggle.mutate(p)}>
                          {p.active ? "Deactivate" : "Activate"}
                        </Button>
                      )}
                    </Td>
                  </Tr>
                  {open === p.id && <tr><td colSpan={6}><TestRun id={p.id} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
