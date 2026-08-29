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
  it("exposes the kanban as a horizontally scrollable region with accessible stages", async () => {
    const user = userEvent.setup();
    renderPage();

    const kanban = screen.getByRole("region", { name: "Воронка продаж" });
    expect(kanban).toHaveClass("kanban--mobile-scroll");
    expect(kanban).toHaveAttribute("tabindex", "0");
    expect(document.getElementById(kanban.getAttribute("aria-describedby")!)).toHaveTextContent("Прокручивайте воронку по горизонтали");

    const firstStage = within(kanban).getByRole("region", { name: "Новый лид" });
    const collapse = within(firstStage).getByRole("button", { name: "Свернуть этап Новый лид" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    await user.click(collapse);
    expect(collapse).toHaveAttribute("aria-expanded", "false");
    expect(within(firstStage).getByText("Анна Смирнова")).not.toBeVisible();
  });

  it("provides stable mobile hooks for the add action and compact list rows", async () => {
    const user = userEvent.setup();
    renderPage();

    const mobileAdd = screen.getAllByRole("button", { name: "Новая сделка" }).find((button) => button.classList.contains("mobile-add"));
    expect(mobileAdd).toHaveClass("mobile-fab");

    await user.click(screen.getAllByRole("button", { name: "Список" })[0]);
    const list = screen.getByRole("region", { name: "Список сделок" });
    const row = within(list).getByRole("button", { name: /^Открыть сделку Кофейня «Слой»\./ });
    expect(row).toHaveClass("deals-list__row");
    expect(row.querySelector(".deals-list__deal")).toHaveAttribute("data-label", "Сделка");
    expect(row.querySelector(".deals-list__stage")).toHaveAttribute("data-label", "Этап");
    expect(row.querySelector(".deals-list__amount")).toHaveAttribute("data-label", "Сумма");
    expect(row.querySelector(".deals-list__source")).toHaveAttribute("data-label", "Источник и срок");
    expect(row.querySelector(".deals-list__owner")).toHaveAttribute("data-label", "Ответственный");
  });

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

    const editTags = within(dialog).getByRole("button", { name: "Изменить теги сделки" });
    expect(editTags).toHaveClass("deal-field-edit", "icon-button");
    expect(editTags).toHaveAttribute("title", "Изменить теги сделки");
    expect(editTags.querySelector("svg")).toBeInTheDocument();
    expect(editTags).not.toHaveTextContent("Изменить");
    await user.click(editTags);
    const input = within(dialog).getByLabelText("Теги сделки");
    await user.clear(input);
    await user.type(input, "Ключевой, Продление, ключевой");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить теги сделки" }));

    expect(within(dialog).getByText("Ключевой", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByText("Продление", { exact: true })).toBeInTheDocument();
    expect(within(dialog).queryAllByText("Ключевой", { exact: true })).toHaveLength(1);
  });
});
