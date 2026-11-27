import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty } from "@/components/ui/table";
import { api } from "@/lib/api";

type Scatter = {
  x: string;
  n: number;
  points: { id: string; t: string | null; s: string | null; p: number; x: number }[];
  fit: { slope: number; intercept: number } | null;
};

const SCALARS: Record<string, { label: string; unit: string }> = {
  mass: { label: "Mass", unit: "kg" },
  volume: { label: "Bounding-box volume", unit: "m³" },
  longest: { label: "Longest dimension", unit: "m" },
  charlen: { label: "Characteristic length (a·b·c)^⅓", unit: "m" },
};

const W = 900, H = 520, PAD = { l: 64, r: 16, t: 16, b: 48 };

function decades(lo: number, hi: number) {
  const out = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) out.push(e);
  return out;
}

function fmtPow(e: number) {
  if (e >= -2 && e <= 4) return String(10 ** e).replace(/\.0+$/, "");
  return `1e${e}`;
}

function LogLog({ data, unit }: { data: Scatter; unit: string }) {
  const lx = data.points.map((p) => Math.log10(p.x));
  const ly = data.points.map((p) => Math.log10(p.p));
  const [x0, x1] = [Math.floor(Math.min(...lx)), Math.ceil(Math.max(...lx)) || 1];
  const [y0, y1] = [Math.floor(Math.min(...ly)), Math.ceil(Math.max(...ly)) || 1];
  const sx = (v: number) => PAD.l + ((v - x0) / Math.max(x1 - x0, 1)) * (W - PAD.l - PAD.r);
  const sy = (v: number) => H - PAD.b - ((v - y0) / Math.max(y1 - y0, 1)) * (H - PAD.t - PAD.b);
  const fit = data.fit;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Price against size, log-log">
      {decades(x0, x1).map((e) => (
        <g key={`x${e}`}>
          <line x1={sx(e)} x2={sx(e)} y1={PAD.t} y2={H - PAD.b} className="stroke-line" />
          <text x={sx(e)} y={H - PAD.b + 18} textAnchor="middle" className="fill-muted text-[11px]">{fmtPow(e)}</text>
        </g>
      ))}
      {decades(y0, y1).map((e) => (
        <g key={`y${e}`}>
          <line x1={PAD.l} x2={W - PAD.r} y1={sy(e)} y2={sy(e)} className="stroke-line" />
          <text x={PAD.l - 8} y={sy(e) + 4} textAnchor="end" className="fill-muted text-[11px]">{fmtPow(e)} €</text>
        </g>
      ))}
      <text x={(W + PAD.l) / 2} y={H - 8} textAnchor="middle" className="fill-muted text-xs">{unit} (log)</text>
      {data.points.map((p, i) => (
        <circle key={p.id} cx={sx(lx[i])} cy={sy(ly[i])} r={3} className="fill-accent/50">
          <title>{`${p.t ?? "?"} (${p.s ?? "?"}): ${p.p.toFixed(2)} €, ${p.x.toPrecision(3)} ${unit}`}</title>
        </circle>
      ))}
      {fit && (
        <line x1={sx(x0)} y1={sy(fit.intercept + fit.slope * x0)} x2={sx(x1)} y2={sy(fit.intercept + fit.slope * x1)}
          className="stroke-bad" strokeWidth={1.5} strokeDasharray="6 4" />
      )}
    </svg>
  );
}

export function ExplorerPage() {
  const [x, setX] = useState("mass");
  const q = useQuery({ queryKey: ["scatter", x], queryFn: () => api.get<Scatter>(`/api/panel/scatter?x=${x}&limit=5000`) });
  const scalar = SCALARS[x];
  return (
    <>
      <PageTitle title="Explorer" subtitle="Price against size, one point per product (canonical price, most trustworthy measurement)" />
      <Card>
        <CardHeader
          title={`Price vs. ${scalar.label.toLowerCase()}`}
          subtitle={q.data ? `${q.data.n.toLocaleString()} products${q.data.fit ? ` · log-log slope ${q.data.fit.slope.toFixed(2)}` : ""}` : "…"}
          action={
            <select value={x} onChange={(e) => setX(e.target.value)} aria-label="Size scalar"
              className="h-8 rounded-md border border-line bg-panel px-2 text-sm">
              {Object.entries(SCALARS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
          }
        />
        <CardBody>
          {q.data && q.data.n > 0 ? <LogLog data={q.data} unit={scalar.unit} /> :
            <Empty>No products with a price and a plausible {scalar.label.toLowerCase()} yet.</Empty>}
        </CardBody>
      </Card>
    </>
  );
}
