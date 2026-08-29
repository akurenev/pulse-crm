import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { CrmProvider } from "../../state/crm-store";
import { DealsPage } from "./DealsPage";

afterEach(cleanup);

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CrmProvider>
          <DealsPage />
        </CrmProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DealsPage", () => {
  it("filters the kanban without mutating the source data", async () => {
    const user = userEvent.setup();
    renderPage();

    const search = screen.getByPlaceholderText("Поиск по сделкам");
    await user.type(search, "Север");

    expect(screen.getByText("ООО Север")).toBeInTheDocument();
    expect(screen.queryByText("Ресторан Парк")).not.toBeInTheDocument();
  });

  it("opens a deal and adds an outgoing message", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("84 000 ₽")).toBeInTheDocument();

    const input = within(dialog).getByPlaceholderText("Написать сообщение");
    await user.type(input, "Предложение готово");
    fireEvent.submit(input.closest("form")!);

    expect(within(dialog).getByText("Предложение готово")).toBeInTheDocument();
  });

  it("shows and edits deal tags", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("HoReCa", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByText("Постоянный клиент", { exact: true })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Изменить теги сделки" }));
    const input = within(dialog).getByLabelText("Теги сделки");
    await user.clear(input);
    await user.type(input, "Ключевой, Продление, ключевой");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить теги сделки" }));

    expect(within(dialog).getByText("Ключевой", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByText("Продление", { exact: true })).toBeInTheDocument();
    expect(within(dialog).queryAllByText("Ключевой", { exact: true })).toHaveLength(1);
  });
});
