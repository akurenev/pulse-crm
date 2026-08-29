import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { CrmProvider } from "../state/crm-store";
import TasksPage from "./TasksPage";

afterEach(cleanup);

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CrmProvider>
        <TasksPage />
      </CrmProvider>
    </QueryClientProvider>,
  );
}

describe("TasksPage", () => {
  it("keeps the desktop segments and mobile picker in sync", async () => {
    const user = userEvent.setup();
    renderPage();

    const mobileFilter = screen.getByRole("combobox", { name: "Статус задач на мобильном" });
    await user.selectOptions(mobileFilter, "overdue");

    expect(screen.getByText("Позвонить по повторному заказу")).toBeInTheDocument();
    expect(screen.queryByText("Отправить предложение")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Просрочено" })).toHaveAttribute("aria-pressed", "true");
  });

  it("opens the creation dialog from the mobile floating action", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Добавить задачу" }));

    expect(await screen.findByRole("dialog", { name: "Новая задача" })).toBeInTheDocument();
  });
});
