import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { Empty, Table, Td, Th, Tr } from "@/components/ui/table";
import { api, type PriceChecks } from "@/lib/api";

const FLAG_LABEL: Record<string, string> = {
  cross_source_outlier: "far from other shops",
  price_disagreement: "two shops disagree",
  price_jump: "jump between visits",
  category_outlier: "category outlier (per kg / m³)",
  review_rejected: "rejected by the LLM review",
};

const VERDICTS = ["valid", "placeholder", "wrong_price_type", "pack_mismatch", "wrong_product", "unclear"];

export function PricesPage() {
  const [verdict, setVerdict] = useState("");
  const data = useQuery({
    queryKey: ["price-checks", verdict],
    queryFn: () => api.get<PriceChecks>(`/api/admin/price-checks?limit=200${verdict ? `&verdict=${verdict}` : ""}`),
    refetchInterval: 60_000,
  });
  const d = data.data;
  return (
    <div className="space-y-4">
      <PageTitle title="Price checks" />
      <p className="text-sm text-muted">
        Offers the checks found suspicious go to the LLM review. A rejection (medium or high confidence) takes
        the offer out of its product's price; every verdict is kept with its version.
      </p>
      {d && (
        <Card>
          <CardHeader title="Flagged offers" />
          <Table>
            <tbody>
              {Object.entries(d.flagged).map(([flag, n]) => (
                <Tr key={flag}><Td>{FLAG_LABEL[flag] ?? flag}</Td><Td className="text-right tabular-nums">{n}</Td></Tr>
              ))}
              {d.reviews.map((r) => (
                <Tr key={`${r.version}/${r.verdict}`}>
                  <Td>review <span className="font-mono text-xs">{r.version}</span>: {r.verdict}</Td>
                  <Td className="text-right tabular-nums">{r.offers}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
      <Card>
        <CardHeader
          title="Latest reviews"
          action={
            <select value={verdict} onChange={(e) => setVerdict(e.target.value)} aria-label="Verdict"
              className="h-8 rounded-md border border-line bg-panel px-2 text-xs">
              <option value="">all verdicts</option>
              {VERDICTS.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          }
        />
        {d && d.latest.length === 0 ? <Empty>No reviews yet.</Empty> : (
          <Table>
            <thead><tr><Th>Product</Th><Th>Shop</Th><Th className="text-right">Price</Th><Th>Verdict</Th><Th>Reason</Th></tr></thead>
            <tbody>
              {d?.latest.map((r) => (
                <Tr key={`${r.offer_id}/${r.at}`}>
                  <Td>{r.url ? <a className="hover:text-accent" href={r.url} target="_blank" rel="noreferrer noopener">{r.title ?? r.url}</a> : r.title}</Td>
                  <Td>{r.source}</Td>
                  <Td className="text-right tabular-nums">{r.price} {r.currency}
                    {r.raw_price && <div className="text-xs text-muted">“{r.raw_price}”</div>}</Td>
                  <Td><Badge tone={r.verdict === "valid" ? "ok" : r.verdict === "unclear" ? "muted" : "bad"}>{r.verdict}</Badge>
                    {r.confidence !== null && <span className="ml-1 text-xs text-muted">{r.confidence}</span>}</Td>
                  <Td className="text-sm">{r.reason}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
