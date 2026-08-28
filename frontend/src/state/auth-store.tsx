/* eslint-disable react-refresh/only-export-components */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type PropsWithChildren } from "react";

import { api, ApiError, remoteEnabled } from "../lib/api";
import type { AuthResponse } from "../types/api";

type AuthStatus = "loading" | "authenticated" | "anonymous";

interface AuthStore {
  status: AuthStatus;
  session: AuthResponse | null;
  login: (email: string, password: string) => Promise<void>;
  acceptInvitation: (token: string, fullName: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const demoSession: AuthResponse = {
  user: {
    id: "user-ak",
    email: "owner@pulse.local",
    full_name: "Алексей Кузнецов",
    role: "owner",
  },
  workspace: {
    id: "workspace-demo",
    name: "Pulse Demo",
    slug: "pulse-demo",
    timezone: "Asia/Yekaterinburg",
    currency: "RUB",
  },
  csrf_token: "",
};

const AuthContext = createContext<AuthStore | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [status, setStatus] = useState<AuthStatus>(remoteEnabled ? "loading" : "authenticated");
  const [session, setSession] = useState<AuthResponse | null>(remoteEnabled ? null : demoSession);

  useEffect(() => {
    if (!remoteEnabled) return;
    let active = true;
    void api.get<AuthResponse>("/auth/me").then((current) => {
      if (!active) return;
      api.setCsrf(current.csrf_token);
      setSession(current);
      setStatus("authenticated");
    }).catch((error: unknown) => {
      if (!active) return;
      if (!(error instanceof ApiError) || error.status !== 401) {
        console.error("Pulse CRM auth bootstrap failed", error);
      }
      api.setCsrf("");
      setSession(null);
      setStatus("anonymous");
    });
    return () => { active = false; };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    if (!remoteEnabled) {
      setSession(demoSession);
      setStatus("authenticated");
      return;
    }
    const current = await api.post<AuthResponse>("/auth/login", { email, password });
    api.setCsrf(current.csrf_token);
    setSession(current);
    setStatus("authenticated");
  }, []);

  const acceptInvitation = useCallback(async (token: string, fullName: string, password: string) => {
    if (!remoteEnabled) {
      const current = { ...demoSession, user: { ...demoSession.user, full_name: fullName } };
      setSession(current);
      setStatus("authenticated");
      return;
    }
    const current = await api.post<AuthResponse>("/auth/accept-invitation", {
      token,
      full_name: fullName,
      password,
    });
    api.setCsrf(current.csrf_token);
    setSession(current);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    if (remoteEnabled) await api.post<void>("/auth/logout", {});
    api.setCsrf("");
    setSession(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, session, login, acceptInvitation, logout }),
    [acceptInvitation, login, logout, session, status],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthStore {
  const store = useContext(AuthContext);
  if (!store) throw new Error("useAuth must be used inside AuthProvider");
  return store;
}
