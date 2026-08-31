import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";

import App from "./App";

const authState = vi.hoisted(() => ({ status: "anonymous" as "anonymous" | "authenticated", session: null as null | {
  user: { id: string; email: string; full_name: string; role: "owner" | "admin" | "manager" | "employee" };
  workspace: { id: string; name: string; slug: string; timezone: string; currency: string };
  csrf_token: string;
} }));

vi.mock("./state/auth-store", () => ({
  useAuth: () => ({ ...authState, login: vi.fn(), logout: vi.fn() }),
}));

function LocationProbe() {
  const location = useLocation();
  const from = location.state && typeof location.state === "object" && "from" in location.state
    ? location.state.from
    : "";
  return <output aria-label="Маршрут входа">{`${location.pathname}|${String(from)}`}</output>;
}

beforeEach(() => {
  authState.status = "anonymous";
  authState.session = null;
});

afterEach(cleanup);

function renderApp(initialEntry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <App />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("authentication routing", () => {
  it("preserves a protected deep link while redirecting an expired session to login", async () => {
    renderApp("/deals?deal=deal-123");

    await waitFor(() => expect(screen.getByRole("status", { name: "Маршрут входа" }))
      .toHaveTextContent("/login|/deals?deal=deal-123"));
  });

  it("redirects employees away from settings", async () => {
    authState.status = "authenticated";
    authState.session = {
      user: { id: "user-employee", email: "employee@example.com", full_name: "Текущий Сотрудник", role: "employee" },
      workspace: { id: "workspace-test", name: "Test", slug: "test", timezone: "UTC", currency: "RUB" },
      csrf_token: "",
    };

    renderApp("/settings");

    await waitFor(() => expect(screen.getByRole("status", { name: "Маршрут входа" })).toHaveTextContent("/deals|"));
    expect(screen.queryByRole("heading", { name: "Настройки" })).not.toBeInTheDocument();
  });
});
