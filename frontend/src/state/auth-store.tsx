/* eslint-disable react-refresh/only-export-components */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError, remoteEnabled } from "../lib/api";
import { cleanupWebPushBeforeLogout } from "../lib/web-push";
import type { AuthResponse } from "../types/api";

type AuthStatus = "loading" | "authenticated" | "anonymous";

interface AuthStore {
  status: AuthStatus;
  session: AuthResponse | null;
  login: (email: string, password: string) => Promise<void>;
  acceptInvitation: (token: string, fullName: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshSession: (options?: { failClosed?: boolean }) => Promise<void>;
  expireSession: () => void;
}

const AUTH_SYNC_STORAGE_KEY = "pulse_auth_expired";

const demoSession: AuthResponse = {
  user: {
    id: "user-ak",
    email: "owner@pulse.local",
    full_name: "Алексей Кузнецов",
    role: "owner",
    version: 1,
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

function sessionIdentity(session: AuthResponse): string {
  return `${session.workspace.id}:${session.user.id}:${session.user.role ?? "none"}`;
}

async function cleanupPushBeforeSessionChange(): Promise<void> {
  try {
    await cleanupWebPushBeforeLogout();
  } catch (error) {
    // Authentication must remain available even if a browser implementation
    // fails while removing a stale subscription from a previous account.
    console.warn("Pulse CRM push session cleanup failed", error);
  }
}

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>(remoteEnabled ? "loading" : "authenticated");
  const [session, setSession] = useState<AuthResponse | null>(remoteEnabled ? null : demoSession);
  const activeIdentity = useRef<string | null>(remoteEnabled ? null : sessionIdentity(demoSession));
  const sessionRefreshGeneration = useRef(0);

  const expireSession = useCallback((broadcast = true) => {
    sessionRefreshGeneration.current += 1;
    queryClient.clear();
    api.setCsrf("");
    setSession(null);
    activeIdentity.current = null;
    setStatus("anonymous");
    if (!broadcast || typeof window === "undefined") return;
    try {
      window.localStorage.setItem(AUTH_SYNC_STORAGE_KEY, `${Date.now()}:${crypto.randomUUID()}`);
    } catch {
      // Storage may be unavailable in private browsing; local expiry still wins.
    }
  }, [queryClient]);

  const applySession = useCallback((current: AuthResponse) => {
    const nextIdentity = sessionIdentity(current);
    if (activeIdentity.current !== null && activeIdentity.current !== nextIdentity) queryClient.clear();
    api.setCsrf(current.csrf_token);
    setSession(current);
    activeIdentity.current = nextIdentity;
    setStatus("authenticated");
  }, [queryClient]);

  const refreshSession = useCallback(async (options?: { failClosed?: boolean }) => {
    if (!remoteEnabled) {
      applySession(demoSession);
      return;
    }
    const failClosed = options?.failClosed === true;
    const generation = ++sessionRefreshGeneration.current;
    if (failClosed) {
      queryClient.clear();
      api.setCsrf("");
      setSession(null);
      setStatus("loading");
    }
    try {
      const current = await api.get<AuthResponse>("/auth/me");
      if (generation !== sessionRefreshGeneration.current) return;
      applySession(current);
    } catch (error) {
      if (generation !== sessionRefreshGeneration.current) return;
      if (failClosed || (error instanceof ApiError && error.status === 401)) {
        expireSession();
        return;
      }
      throw error;
    }
  }, [applySession, expireSession, queryClient]);

  useEffect(() => {
    if (!remoteEnabled) return;
    let active = true;
    void refreshSession({ failClosed: true }).catch((error: unknown) => {
      if (active) console.error("Pulse CRM auth bootstrap failed", error);
    });
    return () => {
      active = false;
      sessionRefreshGeneration.current += 1;
    };
  }, [refreshSession]);

  useEffect(() => {
    if (!remoteEnabled || typeof window === "undefined") return;
    const synchronizeExpiry = (event: StorageEvent) => {
      if (event.key === AUTH_SYNC_STORAGE_KEY && event.newValue) expireSession(false);
    };
    window.addEventListener("storage", synchronizeExpiry);
    return () => window.removeEventListener("storage", synchronizeExpiry);
  }, [expireSession]);

  const login = useCallback(async (email: string, password: string) => {
    if (!remoteEnabled) {
      applySession(demoSession);
      return;
    }
    sessionRefreshGeneration.current += 1;
    await cleanupPushBeforeSessionChange();
    const current = await api.post<AuthResponse>("/auth/login", { email, password });
    applySession(current);
  }, [applySession]);

  const acceptInvitation = useCallback(async (token: string, fullName: string, password: string) => {
    if (!remoteEnabled) {
      const current = { ...demoSession, user: { ...demoSession.user, full_name: fullName } };
      applySession(current);
      return;
    }
    sessionRefreshGeneration.current += 1;
    await cleanupPushBeforeSessionChange();
    const current = await api.post<AuthResponse>("/auth/accept-invitation", {
      token,
      full_name: fullName,
      password,
    });
    applySession(current);
  }, [applySession]);

  const logout = useCallback(async () => {
    sessionRefreshGeneration.current += 1;
    try {
      if (remoteEnabled) {
        await cleanupPushBeforeSessionChange();
        await api.post<void>("/auth/logout", {});
      }
    } finally {
      expireSession();
    }
  }, [expireSession]);

  const value = useMemo(
    () => ({ status, session, login, acceptInvitation, logout, refreshSession, expireSession }),
    [acceptInvitation, expireSession, login, logout, refreshSession, session, status],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthStore {
  const store = useContext(AuthContext);
  if (!store) throw new Error("useAuth must be used inside AuthProvider");
  return store;
}
