import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { AuthProvider, useAuth } from "./auth-store";

const { apiGetMock, apiPostMock, cleanupPushMock, setCsrfMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  cleanupPushMock: vi.fn(),
  setCsrfMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: {
    get: apiGetMock,
    post: apiPostMock,
    setCsrf: setCsrfMock,
  },
  ApiError: class ApiError extends Error {
    constructor(message: string, readonly status: number) {
      super(message);
    }
  },
  remoteEnabled: true,
}));

vi.mock("../lib/web-push", () => ({
  cleanupWebPushBeforeLogout: cleanupPushMock,
}));

const originalLocalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");

function mockLocalStorage() {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
}

function SessionProbe() {
  const { acceptInvitation, login, logout, refreshSession, session, status } = useAuth();
  if (status !== "authenticated") return <span>{status}</span>;
  return (
    <div>
      <output aria-label="Роль">{session?.user.role}</output>
      {session?.user.role === "owner" || session?.user.role === "admin" ? <div>Приватные настройки</div> : null}
      <button type="button" onClick={() => void login("next@example.test", "password")}>Войти заново</button>
      <button type="button" onClick={() => void acceptInvitation("token", "Новый пользователь", "password")}>Принять приглашение</button>
      <button type="button" onClick={() => void refreshSession()}>Проверить сессию</button>
      <button type="button" onClick={() => void refreshSession({ failClosed: true })}>Обновить доступ</button>
      <button type="button" onClick={() => void logout()}>Выйти</button>
    </div>
  );
}

const authResponse = {
  user: { id: "user-test", email: "user@example.test", full_name: "Тест", role: "employee" },
  workspace: { id: "workspace-test", name: "Test", slug: "test", timezone: "UTC", currency: "RUB" },
  csrf_token: "csrf-test",
};

beforeEach(() => {
  mockLocalStorage();
  apiGetMock.mockReset().mockResolvedValue(authResponse);
  apiPostMock.mockReset().mockImplementation((path: string) => (
    path === "/auth/logout" ? Promise.resolve(undefined) : Promise.resolve(authResponse)
  ));
  cleanupPushMock.mockReset().mockResolvedValue(undefined);
  setCsrfMock.mockReset();
});

afterEach(() => {
  cleanup();
  if (originalLocalStorage) Object.defineProperty(window, "localStorage", originalLocalStorage);
});

describe("AuthProvider", () => {
  function renderProvider(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
    return {
      queryClient,
      ...render(<QueryClientProvider client={queryClient}><AuthProvider><SessionProbe /></AuthProvider></QueryClientProvider>),
    };
  }

  it("cleans up the device push subscription before closing the server session", async () => {
    const user = userEvent.setup();
    renderProvider();

    await user.click(await screen.findByRole("button", { name: "Выйти" }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith("/auth/logout", {}));
    expect(cleanupPushMock).toHaveBeenCalledOnce();
    expect(cleanupPushMock.mock.invocationCallOrder[0]).toBeLessThan(apiPostMock.mock.invocationCallOrder[0]);
  });

  it.each([
    ["Войти заново", "/auth/login"],
    ["Принять приглашение", "/auth/accept-invitation"],
  ])("cleans up a previous account subscription before %s", async (buttonName, endpoint) => {
    const user = userEvent.setup();
    renderProvider();

    await user.click(await screen.findByRole("button", { name: buttonName }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(endpoint, expect.any(Object)));
    expect(cleanupPushMock).toHaveBeenCalledOnce();
    expect(cleanupPushMock.mock.invocationCallOrder[0]).toBeLessThan(apiPostMock.mock.invocationCallOrder[0]);
  });

  it.each([
    ["Войти заново", "/auth/login"],
    ["Принять приглашение", "/auth/accept-invitation"],
  ])("does not block %s when stale push cleanup fails", async (buttonName, endpoint) => {
    const user = userEvent.setup();
    const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    cleanupPushMock.mockRejectedValueOnce(new Error("browser cleanup failed"));
    renderProvider();

    await user.click(await screen.findByRole("button", { name: buttonName }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(endpoint, expect.any(Object)));
    expect(warning).toHaveBeenCalledWith("Pulse CRM push session cleanup failed", expect.any(Error));
  });

  it("clears privileged cached data before applying a different identity", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderProvider();
    await screen.findByRole("button", { name: "Войти заново" });
    queryClient.setQueryData(["settings", "pipelines"], [{ id: "admin-only" }]);
    apiPostMock.mockResolvedValueOnce({
      ...authResponse,
      user: { ...authResponse.user, id: "user-next", email: "next@example.test" },
    });

    await user.click(screen.getByRole("button", { name: "Войти заново" }));

    await waitFor(() => expect(queryClient.getQueryData(["settings", "pipelines"])).toBeUndefined());
  });

  it("clears cached workspace data on logout", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderProvider();
    await screen.findByRole("button", { name: "Выйти" });
    queryClient.setQueryData(["companies"], [{ id: "private-company" }]);

    await user.click(screen.getByRole("button", { name: "Выйти" }));

    await waitFor(() => expect(queryClient.getQueryData(["companies"])).toBeUndefined());
  });

  it("expires the local identity and clears private cache when a session probe returns 401", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderProvider();
    await screen.findByRole("button", { name: "Проверить сессию" });
    queryClient.setQueryData(["contacts"], [{ id: "private-contact" }]);
    apiGetMock.mockRejectedValueOnce(new ApiError("expired", 401));

    await user.click(screen.getByRole("button", { name: "Проверить сессию" }));

    await waitFor(() => expect(screen.getByText("anonymous")).toBeInTheDocument());
    expect(queryClient.getQueryData(["contacts"])).toBeUndefined();
    expect(screen.queryByRole("button", { name: "Проверить сессию" })).not.toBeInTheDocument();
  });

  it("fails closed while refreshing access and applies a demoted role", async () => {
    const ownerResponse = {
      ...authResponse,
      user: { ...authResponse.user, role: "owner" as const },
    };
    let resolveDemoted!: (value: typeof authResponse) => void;
    const demotedResponse = new Promise<typeof authResponse>((resolve) => { resolveDemoted = resolve; });
    apiGetMock.mockResolvedValueOnce(ownerResponse);
    const user = userEvent.setup();
    const { queryClient } = renderProvider();
    expect(await screen.findByText("Приватные настройки")).toBeInTheDocument();
    queryClient.setQueryData(["settings", "pipelines"], [{ id: "private-pipeline" }]);
    apiGetMock.mockReturnValueOnce(demotedResponse);

    await user.click(screen.getByRole("button", { name: "Обновить доступ" }));

    expect(screen.getByText("loading")).toBeInTheDocument();
    expect(screen.queryByText("Приватные настройки")).not.toBeInTheDocument();
    expect(queryClient.getQueryData(["settings", "pipelines"])).toBeUndefined();
    resolveDemoted(authResponse);
    await waitFor(() => expect(screen.getByLabelText("Роль")).toHaveTextContent("employee"));
    expect(screen.queryByText("Приватные настройки")).not.toBeInTheDocument();
  });

  it("ignores an older session probe that resolves after access reconciliation", async () => {
    let resolveOlder!: (value: typeof authResponse) => void;
    let resolveLatest!: (value: typeof authResponse) => void;
    const olderProbe = new Promise<typeof authResponse>((resolve) => { resolveOlder = resolve; });
    const latestProbe = new Promise<typeof authResponse>((resolve) => { resolveLatest = resolve; });
    const user = userEvent.setup();
    renderProvider();
    await screen.findByRole("button", { name: "Проверить сессию" });
    apiGetMock.mockReturnValueOnce(olderProbe).mockReturnValueOnce(latestProbe);

    await user.click(screen.getByRole("button", { name: "Проверить сессию" }));
    await user.click(screen.getByRole("button", { name: "Обновить доступ" }));
    resolveLatest(authResponse);
    await waitFor(() => expect(screen.getByLabelText("Роль")).toHaveTextContent("employee"));

    resolveOlder({
      ...authResponse,
      user: { ...authResponse.user, role: "owner" },
    });
    await Promise.resolve();

    expect(screen.getByLabelText("Роль")).toHaveTextContent("employee");
    expect(screen.queryByText("Приватные настройки")).not.toBeInTheDocument();
  });
});
