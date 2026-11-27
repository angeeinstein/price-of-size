import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function LoginPage() {
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/panel/login", { username, password, totp: totp || null });
      await refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        const d = err.detail as { totp_required?: boolean } | undefined;
        if (d?.totp_required) setNeedTotp(true);
        setError(err.status === 429 ? "Too many attempts. Try again in a few minutes." : err.message);
      } else setError("Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid h-full place-items-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border border-line bg-panel p-8 shadow-sm">
        <div>
          <h1 className="text-lg font-semibold">The Price of Size</h1>
          <p className="text-sm text-muted">Sign in to the control panel</p>
        </div>
        <div>
          <Label>Username</Label>
          <Input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" name="username" />
        </div>
        <div>
          <Label>Password</Label>
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" name="password" />
        </div>
        {needTotp && (
          <div>
            <Label>Authenticator code</Label>
            <Input inputMode="numeric" autoFocus value={totp} onChange={(e) => setTotp(e.target.value)} autoComplete="one-time-code" name="totp" />
          </div>
        )}
        {error && <p className="text-sm text-bad" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
      </form>
    </div>
  );
}
