import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { api, ApiError, setCsrf, type Me } from "./api";

type AuthState = {
  me: Me | null;
  loading: boolean;
  refresh: () => Promise<void>;
  // Run a sensitive action; if the server asks for re-authentication, prompt, then retry.
  sensitive: <T>(fn: () => Promise<T>) => Promise<T>;
  reauthPrompt: { resolve: (ok: boolean) => void } | null;
  setReauthPrompt: (p: { resolve: (ok: boolean) => void } | null) => void;
};

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        const me = await api.get<Me>("/api/panel/me");
        setCsrf(me.csrf_token);
        return me;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
  const [reauthPrompt, setReauthPrompt] = useState<AuthState["reauthPrompt"]>(null);

  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: ["me"] });
  }, [qc]);

  const sensitive = useCallback(async <T,>(fn: () => Promise<T>): Promise<T> => {
    try {
      return await fn();
    } catch (e) {
      if (!(e instanceof ApiError) || !e.reauthRequired) throw e;
      const ok = await new Promise<boolean>((resolve) => setReauthPrompt({ resolve }));
      setReauthPrompt(null);
      if (!ok) throw e;
      return await fn();
    }
  }, []);

  return (
    <Ctx.Provider value={{ me: q.data ?? null, loading: q.isLoading, refresh, sensitive,
      reauthPrompt, setReauthPrompt }}>
      {children}
    </Ctx.Provider>
  );
}

export function useAuth() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

export function useIsAdmin() {
  return useAuth().me?.user.role === "admin";
}
