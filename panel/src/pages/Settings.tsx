import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";

const HELP: Record<string, string> = {
  respect_robots_txt: "Enforce robots.txt rules (it is always fetched and stored)",
  rate_limit_scope: "per_egress_ip: each public IP gets its own per-domain budget; global: one budget",
  base_rate_rps: "Start rate per domain (0.2 = one request every 5 s)",
  max_rate_rps: "Ceiling the adaptive rate never exceeds",
  max_total_rate_per_domain: "Optional hard cap per domain across all egress groups",
  llm_budget_hard_cap_usd: "Monthly LLM spending cap; LLM stages pause at 100 %",
  llm_budget_soft_target_usd: "Optional lower monthly target for pacing",
  html_archive_quota_gb: "HTML archive size before switching to sample-only",
  user_agent: "How the crawler identifies itself to shops",
  block_challenge_run: "Challenge pages (CAPTCHA, bot wall) in a row before a domain is blocked",
  block_suspect_pause_s: "Pause after the first challenge page, in seconds",
  block_forbidden_run: "HTTP 403 answers in a row that count as a block",
  block_retry_after_h: "Try a blocked domain again after this many hours (null: only when unblocked by hand)",
};

function Row({ k, value }: { k: string; value: unknown }) {
  const qc = useQueryClient();
  const admin = useIsAdmin();
  const { sensitive } = useAuth();
  const [text, setText] = useState(JSON.stringify(value));
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      let parsed: unknown;
      try { parsed = JSON.parse(text); } catch { throw new Error('enter JSON, e.g. 0.5, true, "text", null'); }
      return sensitive(() => api.put(`/api/admin/settings/${encodeURIComponent(k)}`, { value: parsed as never }));
    },
    onSuccess: () => { setError(null); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : String(e)),
  });
  const changed = text !== JSON.stringify(value);
  return (
    <Tr>
      <Td className="w-80"><div className="font-mono text-xs">{k}</div>{HELP[k] && <div className="text-xs text-muted">{HELP[k]}</div>}</Td>
      <Td>
        <div className="flex items-center gap-2">
          <Input className="h-8 font-mono text-xs" value={text} disabled={!admin} onChange={(e) => setText(e.target.value)} aria-label={k} />
          {admin && changed && <Button size="sm" onClick={() => save.mutate()}>Save</Button>}
        </div>
        {error && <div className="mt-1 text-xs text-bad">{error}</div>}
      </Td>
    </Tr>
  );
}

export function SettingsPage() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Record<string, unknown>>("/api/admin/settings") });
  return (
    <>
      <PageTitle title="Settings" subtitle="Runtime switches; changes apply within seconds and are audit-logged" />
      <Card>
        <CardHeader title="Settings" />
        <Table>
          <thead><tr><Th>Key</Th><Th>Value (JSON)</Th></tr></thead>
          <tbody>
            {Object.entries(settings.data ?? {}).map(([k, v]) => <Row key={`${k}:${JSON.stringify(v)}`} k={k} value={v} />)}
          </tbody>
        </Table>
      </Card>
    </>
  );
}
