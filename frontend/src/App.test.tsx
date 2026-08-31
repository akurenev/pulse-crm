import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";

import App from "./App";

vi.mock("./state/auth-store", () => ({
  useAuth: () => ({ status: "anonymous", session: null, login: vi.fn() }),
}));

function LocationProbe() {
  const location = useLocation();
  const from = location.state && typeof location.state === "object" && "from" in location.state
    ? location.state.from
    : "";
  return <output aria-label="Маршрут входа">{`${location.pathname}|${String(from)}`}</output>;
}

afterEach(cleanup);

describe("authentication routing", () => {
  it("preserves a protected deep link while redirecting an expired session to login", async () => {
    render(
      <MemoryRouter initialEntries={["/deals?deal=deal-123"]}>
        <App />
        <LocationProbe />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByRole("status", { name: "Маршрут входа" }))
      .toHaveTextContent("/login|/deals?deal=deal-123"));
  });
});
