import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";
import { cn, fmtAgo, fmtTime } from "@/lib/utils";

// One-click updates (coordinator/updates.py): what GitHub has, and the rollout:
// the coordinator first, then one node, then the others.

interface Rollout {
  id: string;
  version: string;
  from_version: string | null;
  status: "coordinator" | "canary" | "nodes" | "done" | "failed" | "cancelled";
  canary_node_id: string | null;
  error: string | null;
  started_by: string;
  started_at: string;
  finished_at: string | null;
}

interface NodeState {
  id: string;
  name: string;
  online: boolean;
  managed: boolean;
  version: string | null;
  target: string | null;
  update_state: string | null;
  update_target: string | null;
  update_error: string | null;
}

export interface UpdatesOverview {
  current: string;
  repository: string;
  self_update: boolean;
  check: {
    checked_at: string;
    error: string | null;
    latest: { version: string; tag: string; url: string | null; notes: string; published_at: string | null } | null;
  } | null;
  update_available: boolean;
  coordinator_agent: { running_version: string | null; update_state: string | null; update_target: string | null; update_error: string | null } | null;
  rollouts: Rollout[];
  nodes: NodeState[];
}

const ACTIVE = ["coordinator", "canary", "nodes"];

// what an agent is doing (pos_agent manager phases)
const STEP: Record<string, string> = {
  pulling: "downloading the new images",
  selftest: "checking the new version",
  preparing: "preparing",
  backing_up: "backing up the database",
  migrating: "migrating the database",
  draining: "stopping the old version (finishing its jobs)",
  starting: "starting the new version",
  health_check: "waiting until the new version answers",
  agent_update: "updating its agent",
};

export function useUpdates(fast = false) {
  return useQuery({
    queryKey: ["updates"],
    queryFn: () => api.get<UpdatesOverview>("/api/admin/updates"),
    refetchInterval: (q) => {
      const active = q.state.data?.rollouts.some((r) => ACTIVE.includes(r.status));
      return active || fast ? 2000 : 60_000;
    },
    retry: false,
  });
}

function Step({ state, title, children }: { state: "done" | "active" | "waiting" | "failed"; title: string; children?: ReactNode }) {
  const icon = {
    done: <CheckCircle2 size={18} className="text-ok" />,
    active: <Loader2 size={18} className="animate-spin text-accent" />,
    waiting: <CircleDashed size={18} className="text-muted" />,
    failed: <XCircle size={18} className="text-bad" />,
  }[state];
  return (
    <li className="flex gap-3">
      <div className="pt-0.5">{icon}</div>
      <div className="min-w-0">
        <div className={cn("text-sm", state === "waiting" ? "text-muted" : "font-medium")}>{title}</div>
        {children && <div className="text-xs text-muted">{children}</div>}
      </div>
    </li>
  );
}

function nodeLine(n: NodeState, version: string): string {
  if (n.version === version) return `running ${version}`;
  if (n.update_state === "failed" && n.update_target === version) return `failed: ${n.update_error ?? "update failed"}`;
  if (n.update_state && STEP[n.update_state]) return STEP[n.update_state];
  if (!n.online) return "offline: updates when it is back";
  if (n.target === version) return "waiting for its agent";
  return `running ${n.version ?? "?"}`;
}

function RolloutView({ r, data }: { r: Rollout; data: UpdatesOverview }) {
  const agent = data.coordinator_agent;
  const managed = data.nodes.filter((n) => n.managed);
  const canary = managed.find((n) => n.id === r.canary_node_id);
  const others = managed.filter((n) => n.id !== r.canary_node_id);
  const othersDone = others.filter((n) => n.version === r.version).length;
  // the step reached: 0 coordinator, 1 first node, 2 other nodes, 3 all done
  const at =
    r.status === "done" ? 3
    : r.status === "coordinator" ? 0
    : r.status === "canary" ? 1
    : r.status === "nodes" ? 2
    // failed or cancelled: where it stopped (a coordinator failure says so)
    : r.status === "failed" ? (r.error?.startsWith("coordinator:") ? 0 : canary && canary.version !== r.version ? 1 : 2)
    : data.current !== r.version ? 0
    : canary && canary.version !== r.version ? 1
    : 2;
  const state = (i: number): "done" | "active" | "waiting" | "failed" =>
    i < at ? "done"
    : i > at ? "waiting"
    : ACTIVE.includes(r.status) ? "active"
    : r.status === "failed" ? "failed"
    : "waiting";

  let coordinatorText: string;
  if (at > 0) coordinatorText = `running ${r.version}`;
  else if (r.status === "failed") coordinatorText = r.error ?? "failed";
  else if (agent?.update_target === r.version && agent.update_state && STEP[agent.update_state]) coordinatorText = STEP[agent.update_state];
  else coordinatorText = "waiting for its agent (it checks every few seconds)";

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">{r.from_version ?? "?"} → {r.version}</span>
        {r.status === "done" && <Badge tone="ok">done</Badge>}
        {r.status === "failed" && <Badge tone="bad">failed</Badge>}
        {r.status === "cancelled" && <Badge tone="muted">cancelled</Badge>}
        {ACTIVE.includes(r.status) && <Badge tone="accent">running</Badge>}
        <span className="text-xs text-muted">started {fmtAgo(r.started_at)} by {r.started_by}</span>
      </div>
      <ol className="space-y-3">
        <Step state={state(0)} title="Coordinator">{coordinatorText}</Step>
        <Step state={state(1)} title={canary ? `First node: ${canary.name}` : "First node"}>
          {canary ? nodeLine(canary, r.version) : at > 1 ? "no node was online to try it on first" : "after the coordinator"}
        </Step>
        <Step state={state(2)} title={`Other nodes${at >= 2 && others.length ? ` (${othersDone} of ${others.length})` : ""}`}>
          {at >= 2
            ? others.length
              ? others.map((n) => <div key={n.id}>{n.name}: {nodeLine(n, r.version)}</div>)
              : "none"
            : "after the first node"}
        </Step>
      </ol>
      {r.status === "failed" && r.error && <p className="text-sm text-bad">{r.error}</p>}
      {r.status === "failed" && (
        <p className="text-xs text-muted">
          The agents rolled back to the version before. An agent tries a failed version again after 6 hours;
          the logs are on the Nodes page (Fetch logs) and in <code>docker logs pos-agent-coordinator</code>.
        </p>
      )}
    </div>
  );
}

export function UpdatesCard() {
  const admin = useIsAdmin();
  const qc = useQueryClient();
  const { sensitive } = useAuth();
  const q = useUpdates();
  const [error, setError] = useState<string | null>(null);
  const done = (data: UpdatesOverview) => { setError(null); qc.setQueryData(["updates"], data); qc.invalidateQueries({ queryKey: ["releases"] }); };
  const fail = (e: unknown) => setError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
  const check = useMutation({ mutationFn: () => api.post<UpdatesOverview>("/api/admin/updates/check"), onSuccess: done, onError: fail });
  const start = useMutation({
    mutationFn: (version: string) => sensitive(() => api.post<UpdatesOverview>("/api/admin/updates/start", { version })),
    onSuccess: done, onError: fail,
  });
  const cancel = useMutation({
    mutationFn: () => sensitive(() => api.post<UpdatesOverview>("/api/admin/updates/cancel")),
    onSuccess: done, onError: fail,
  });

  const data = q.data;
  const latest = data?.check?.latest ?? null;
  const rollout = data?.rollouts[0];
  const active = !!rollout && ACTIVE.includes(rollout.status);
  // while the coordinator switches versions it does not answer for a few seconds
  const reconnecting = q.isError && active;

  return (
    <Card className="mb-6">
      <CardHeader
        title="Updates"
        subtitle={data ? `Running ${data.current} · releases from github.com/${data.repository}` : "…"}
        action={admin && (
          <Button size="sm" variant="outline" disabled={check.isPending} onClick={() => check.mutate()}>
            {check.isPending ? "Checking…" : "Check now"}
          </Button>
        )}
      />
      <CardBody className="space-y-4">
        {data && !data.check && <p className="text-sm text-muted">Not checked yet (the coordinator asks GitHub every few hours, or now with "Check now").</p>}
        {data?.check?.error && <p className="text-sm text-bad">{data.check.error}</p>}
        {latest && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {data!.update_available
              ? <><Badge tone="accent">update available</Badge> <span><span className="font-medium">{latest.version}</span> on GitHub</span></>
              : <><Badge tone="ok">up to date</Badge> <span>latest on GitHub: {latest.version}</span></>}
            {latest.published_at && <span className="text-xs text-muted">published {fmtTime(latest.published_at)}</span>}
            {latest.url && <a className="text-xs text-accent hover:underline" href={latest.url} target="_blank" rel="noreferrer">release page</a>}
            <span className="text-xs text-muted">checked {fmtAgo(data!.check!.checked_at)}</span>
          </div>
        )}
        {latest?.notes && data!.update_available && (
          <details className="text-sm">
            <summary className="cursor-pointer text-muted">What changed</summary>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 text-xs">{latest.notes}</pre>
          </details>
        )}
        {data && !data.self_update && (
          <p className="text-sm text-muted">This coordinator is not run by its agent, so it cannot update itself: update it with <code>install.sh --update</code>.</p>
        )}
        {admin && data?.self_update && latest && data.update_available && !active && (
          <Button disabled={start.isPending} onClick={() => {
            if (confirm(`Update everything to ${latest.version}?\n\nThe coordinator first (backup if the database changes, migrations, switch), then one node, then the others. Running jobs are finished or handed back, nothing is lost.`))
              start.mutate(latest.version);
          }}>
            {start.isPending ? "Starting…" : `Update to ${latest.version}`}
          </Button>
        )}
        {error && <p className="text-sm text-bad">{error}</p>}
        {reconnecting && <p className="text-sm text-muted">The coordinator is switching versions; reconnecting…</p>}
        {rollout && data && (active || (rollout.finished_at && Date.now() - new Date(rollout.finished_at).getTime() < 24 * 3600_000)) && (
          <div className="rounded-lg border border-line p-4">
            <RolloutView r={rollout} data={data} />
            {admin && active && (
              <Button className="mt-3" size="sm" variant="ghost" disabled={cancel.isPending} onClick={() => {
                if (confirm("Stop the update? A coordinator or node that is switching right now finishes its step; nodes not started yet stay on their version."))
                  cancel.mutate();
              }}>Stop the rollout</Button>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

// A one-line pointer for other pages (Settings).
export function UpdatesHint() {
  const q = useUpdates();
  if (!q.data) return null;
  const active = q.data.rollouts.some((r) => ACTIVE.includes(r.status));
  return (
    <Card className="mb-6">
      <CardBody className="flex flex-wrap items-center gap-3 py-3 text-sm">
        <span>Version <span className="font-medium">{q.data.current}</span></span>
        {active ? <Badge tone="accent">updating</Badge> : q.data.update_available
          ? <Badge tone="accent">{q.data.check?.latest?.version} available</Badge>
          : q.data.check?.latest ? <Badge tone="ok">up to date</Badge> : null}
        <Link className="ml-auto text-accent hover:underline" to="/releases">Updates and releases →</Link>
      </CardBody>
    </Card>
  );
}
