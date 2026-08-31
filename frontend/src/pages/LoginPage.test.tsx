import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import LoginPage from "./LoginPage";

const { loginMock } = vi.hoisted(() => ({ loginMock: vi.fn() }));

vi.mock("../state/auth-store", () => ({
  useAuth: () => ({ login: loginMock }),
}));

function Destination() {
  const location = useLocation();
  return <h1>{`${location.pathname}${location.search}`}</h1>;
}

function renderLogin(from: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/login", state: { from } }]}>
      <Routes>
        <Route path="login" element={<LoginPage />} />
        <Route path="deals" element={<Destination />} />
        <Route path="tasks" element={<Destination />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  loginMock.mockReset().mockResolvedValue(undefined);
});

afterEach(cleanup);

describe("LoginPage return navigation", () => {
  it("returns to a safe task deep link after authentication", async () => {
    const user = userEvent.setup();
    renderLogin("/tasks?task=task-123");

    await user.click(screen.getByRole("button", { name: /Войти/ }));

    expect(await screen.findByRole("heading", { name: "/tasks?task=task-123" })).toBeInTheDocument();
  });

  it("falls back to deals when navigation state is external", async () => {
    const user = userEvent.setup();
    renderLogin("https://attacker.example/tasks?task=task-123");

    await user.click(screen.getByRole("button", { name: /Войти/ }));

    expect(await screen.findByRole("heading", { name: "/deals" })).toBeInTheDocument();
  });
});
