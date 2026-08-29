import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CrmProvider } from "../state/crm-store";
import SettingsPage from "./SettingsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CrmProvider>
          <SettingsPage />
        </CrmProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SettingsPage custom fields", () => {
  it("removes a deal field from the active catalog after confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByText("Сегмент клиента")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Удалить поле сделки Сегмент клиента" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("сохранённые значения сделок останутся в базе"));
    expect(screen.queryByText("Сегмент клиента")).not.toBeInTheDocument();
    expect(screen.getByText("Договор подписан")).toBeInTheDocument();
  });
});
