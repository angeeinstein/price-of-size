import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

// Sensitive actions (activate a release, revoke a node, change settings...) need the
// password (and TOTP) again if the last login/re-auth is older than 10 minutes.
export function ReauthDialog() {
  const { reauthPrompt, me } = useAuth();
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!reauthPrompt) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/panel/reauth", { password, totp: totp || null });
      setPassword(""); setTotp("");
      reauthPrompt!.resolve(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "failed");
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" role="dialog" aria-modal="true">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border border-line bg-panel p-6 shadow-xl">
        <div>
          <h2 className="font-semibold">Confirm it's you</h2>
          <p className="text-sm text-muted">This action needs your password again.</p>
        </div>
        <div>
          <Label>Password</Label>
          <Input type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </div>
        {me?.user.totp && (
          <div>
            <Label>Authenticator code</Label>
            <Input inputMode="numeric" value={totp} onChange={(e) => setTotp(e.target.value)} autoComplete="one-time-code" />
          </div>
        )}
        {error && <p className="text-sm text-bad">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={() => reauthPrompt.resolve(false)}>Cancel</Button>
          <Button type="submit">Confirm</Button>
        </div>
      </form>
    </div>
  );
}
