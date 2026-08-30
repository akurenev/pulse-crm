import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

function SessionProbe() {
  const { acceptInvitation, login, logout, status } = useAuth();
  if (status !== "authenticated") return <span>{status}</span>;
  return (
    <div>
      <button type="button" onClick={() => void login("next@example.test", "password")}>Войти заново</button>
      <button type="button" onClick={() => void acceptInvitation("token", "Новый пользователь", "password")}>Принять приглашение</button>
      <button type="button" onClick={() => void logout()}>Выйти</button>
    </div>
  );
}

const authResponse = {
  user: { id: "user-test", email: "user@example.test", full_name: "Тест", role: "member" },
  workspace: { id: "workspace-test", name: "Test", slug: "test", timezone: "UTC", currency: "RUB" },
  csrf_token: "csrf-test",
};

beforeEach(() => {
  apiGetMock.mockReset().mockResolvedValue(authResponse);
  apiPostMock.mockReset().mockImplementation((path: string) => (
    path === "/auth/logout" ? Promise.resolve(undefined) : Promise.resolve(authResponse)
  ));
  cleanupPushMock.mockReset().mockResolvedValue(undefined);
  setCsrfMock.mockReset();
});

afterEach(cleanup);

describe("AuthProvider", () => {
  it("cleans up the device push subscription before closing the server session", async () => {
    const user = userEvent.setup();
    render(<AuthProvider><SessionProbe /></AuthProvider>);

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
    render(<AuthProvider><SessionProbe /></AuthProvider>);

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
    render(<AuthProvider><SessionProbe /></AuthProvider>);

    await user.click(await screen.findByRole("button", { name: buttonName }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(endpoint, expect.any(Object)));
    expect(warning).toHaveBeenCalledWith("Pulse CRM push session cleanup failed", expect.any(Error));
  });
});
