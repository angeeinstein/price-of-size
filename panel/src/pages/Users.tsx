import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError, type User } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtAgo } from "@/lib/utils";

export function UsersPage() {
  const qc = useQueryClient();
  const { sensitive, me } = useAuth();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.get<User[]>("/api/panel/users") });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () =>
    api.get<{ at: string; actor: string; action: string; target: string | null }[]>("/api/admin/audit?limit=50") });
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" });
  const [error, setError] = useState<string | null>(null);
  const done = () => { setError(null); qc.invalidateQueries({ queryKey: ["users"] }); };
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : String(e));
  const create = useMutation({
    mutationFn: () => sensitive(() => api.post("/api/panel/users", form)),
    onSuccess: () => { setForm({ username: "", password: "", role: "viewer" }); done(); },
    onError: fail,
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      sensitive(() => api.patch(`/api/panel/users/${id}`, body)),
    onSuccess: done, onError: fail,
  });

  return (
    <>
      <PageTitle title="Users" subtitle="Admins change things; viewers only look" />
      <Card className="mb-6">
        <CardHeader title="Add user" />
        <CardBody className="flex flex-wrap items-end gap-3">
          <div className="w-48"><Label>Username</Label><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></div>
          <div className="w-64"><Label>Initial password (min. 12)</Label><Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></div>
          <div><Label>Role</Label>
            <select className="h-9 rounded-md border border-line bg-panel px-2 text-sm" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              <option>viewer</option><option>admin</option>
            </select></div>
          <Button disabled={!form.username || !form.password} onClick={() => create.mutate()}>Create</Button>
          {error && <span className="text-sm text-bad">{error}</span>}
        </CardBody>
      </Card>
      <Card className="mb-6">
        <Table>
          <thead><tr><Th>User</Th><Th>Role</Th><Th>Second factor</Th><Th>Last login</Th><Th /></tr></thead>
          <tbody>
            {users.data?.map((u) => (
              <Tr key={u.id}>
                <Td className="font-medium">{u.username}{u.disabled && <Badge tone="bad">disabled</Badge>}</Td>
                <Td><Badge tone={u.role === "admin" ? "accent" : "muted"}>{u.role}</Badge></Td>
                <Td>{u.totp ? <Badge tone="ok">TOTP</Badge> : <Badge tone="warn">none</Badge>}</Td>
                <Td className="text-muted">{fmtAgo(u.last_login_at)}</Td>
                <Td className="space-x-2 whitespace-nowrap text-right">
                  {u.username !== me?.user.username && (
                    <>
                      <Button size="sm" variant="outline" onClick={() => update.mutate({ id: u.id!, body: { role: u.role === "admin" ? "viewer" : "admin" } })}>
                        Make {u.role === "admin" ? "viewer" : "admin"}</Button>
                      <Button size="sm" variant="outline" onClick={() => update.mutate({ id: u.id!, body: { disabled: !u.disabled } })}>
                        {u.disabled ? "Enable" : "Disable"}</Button>
                      {u.totp && <Button size="sm" variant="ghost" onClick={() => update.mutate({ id: u.id!, body: { reset_totp: true } })}>Reset TOTP</Button>}
                    </>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </Card>
      <Card>
        <CardHeader title="Audit log" subtitle="Every admin action" />
        <Table>
          <tbody>
            {audit.data?.map((a, i) => (
              <Tr key={i}><Td className="whitespace-nowrap text-muted">{fmtAgo(a.at)}</Td><Td>{a.actor}</Td><Td>{a.action}</Td><Td className="text-muted">{a.target}</Td></Tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </>
  );
}
