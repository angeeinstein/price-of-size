import { useState } from "react";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

function msgOf(e: unknown) {
  return e instanceof ApiError ? e.message : String(e);
}

export function AccountPage() {
  const { me, refresh, sensitive } = useAuth();
  const [pw, setPw] = useState({ current: "", new: "" });
  const [pwMsg, setPwMsg] = useState<string | null>(null);
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [totpMsg, setTotpMsg] = useState<string | null>(null);

  async function changePassword() {
    try {
      await api.post("/api/panel/me/password", pw);
      setPw({ current: "", new: "" });
      setPwMsg("Password changed. Other sessions were signed out.");
    } catch (e) { setPwMsg(msgOf(e)); }
  }
  async function startTotp() {
    try { setSetup(await sensitive(() => api.post("/api/panel/me/totp/setup"))); setTotpMsg(null); }
    catch (e) { setTotpMsg(msgOf(e)); }
  }
  async function enableTotp() {
    try { await api.post("/api/panel/me/totp/enable", { code }); setSetup(null); setCode(""); await refresh(); setTotpMsg("Two-factor sign-in is on."); }
    catch (e) { setTotpMsg(msgOf(e)); }
  }
  async function disableTotp() {
    if (!confirm("Turn off two-factor sign-in?")) return;
    try { await sensitive(() => api.post("/api/panel/me/totp/disable")); await refresh(); setTotpMsg("Two-factor sign-in is off."); }
    catch (e) { setTotpMsg(msgOf(e)); }
  }

  if (!me) return null;
  return (
    <>
      <PageTitle title="Account" subtitle={`${me.user.username} · ${me.user.role}`} />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Password" />
          <CardBody className="space-y-3">
            <div><Label>Current password</Label><Input type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></div>
            <div><Label>New password (at least 12 characters)</Label><Input type="password" autoComplete="new-password" value={pw.new} onChange={(e) => setPw({ ...pw, new: e.target.value })} /></div>
            {pwMsg && <p className="text-sm text-muted">{pwMsg}</p>}
            <Button disabled={!pw.current || !pw.new} onClick={changePassword}>Change password</Button>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Two-factor sign-in" action={me.user.totp ? <Badge tone="ok">on</Badge> : <Badge tone="warn">off</Badge>} />
          <CardBody className="space-y-3">
            {!me.user.totp && !setup && <Button onClick={startTotp}>Set up an authenticator app</Button>}
            {setup && (
              <>
                <p className="text-sm">Add this key to your authenticator app (or open the link on your phone), then enter the 6-digit code.</p>
                <code className="block break-all rounded bg-line p-2 text-xs">{setup.secret}</code>
                <a className="block break-all text-xs text-accent underline" href={setup.otpauth_uri}>{setup.otpauth_uri}</a>
                <div className="flex gap-2">
                  <Input inputMode="numeric" className="w-32" value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" aria-label="Code" />
                  <Button onClick={enableTotp} disabled={code.length < 6}>Confirm</Button>
                </div>
              </>
            )}
            {me.user.totp && <Button variant="outline" onClick={disableTotp}>Turn off</Button>}
            {totpMsg && <p className="text-sm text-muted">{totpMsg}</p>}
          </CardBody>
        </Card>
      </div>
    </>
  );
}
