import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DndContext } from "@dnd-kit/core";
import type { ComponentProps } from "react";

import { initialDeals, pipeline as demoPipeline, users as demoUsers } from "../../data/demo";
import { ApiError } from "../../lib/api";
import { CrmProvider } from "../../state/crm-store";
import { DealDrawer } from "./DealDrawer";
import { DealsPage } from "./DealsPage";
import { StageColumn } from "./StageColumn";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output aria-label="Текущий адрес">{`${location.pathname}${location.search}`}</output>
    <button type="button" onClick={() => navigate(-1)}>Назад в истории</button>
  </>;
}

function renderPage(initialEntry = "/deals", providerProps: Partial<ComponentProps<typeof CrmProvider>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <CrmProvider {...providerProps}>
          <DealsPage />
          <LocationProbe />
        </CrmProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderDrawer(overrides: Partial<ComponentProps<typeof DealDrawer>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props: ComponentProps<typeof DealDrawer> = {
    deal: initialDeals[0],
    pipeline: demoPipeline,
    assignees: Object.values(demoUsers),
    mutationPending: false,
    onClose: vi.fn(),
    onMove: vi.fn().mockResolvedValue(undefined),
    onSetNextPurchase: vi.fn().mockResolvedValue(undefined),
    onSetContact: vi.fn().mockResolvedValue(undefined),
    onSetCompany: vi.fn().mockResolvedValue(undefined),
    onSetAssignee: vi.fn().mockResolvedValue(undefined),
    onSetTags: vi.fn().mockResolvedValue(undefined),
    onSetCustomFields: vi.fn().mockResolvedValue(undefined),
    onSendMessage: vi.fn().mockResolvedValue(undefined),
    onRetryMessage: vi.fn().mockResolvedValue(undefined),
    onToggleTask: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <button type="button">Внешнее действие</button>
      <DealDrawer {...props} />
    </QueryClientProvider>,
  );
}

describe("DealsPage", () => {
  it("opens a deal from a deep-link URL and clears the route when the drawer closes", async () => {
    const user = userEvent.setup();
    const deal = initialDeals.find((item) => item.id === "deal-sloy")!;
    renderPage(`/deals?deal=${deal.id}&view=compact`);

    const dialog = await screen.findByRole("dialog", { name: deal.title });
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent(`/deals?deal=${deal.id}&view=compact`);

    await user.click(within(dialog).getByRole("button", { name: "Закрыть карточку" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: deal.title })).not.toBeInTheDocument());
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent("/deals?view=compact");
  });

  it("closes the selected deal when browser history removes its query parameter", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    expect(await screen.findByRole("dialog", { name: "Кофейня «Слой»" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent("deal=deal-sloy");

    await user.click(screen.getByRole("button", { name: "Назад в истории" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Кофейня «Слой»" })).not.toBeInTheDocument());
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent(/^\/deals$/);
  });

  it("adds the created deal to the URL before opening its drawer", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getAllByRole("button", { name: "Новая сделка" })[0]);
    const createDialog = await screen.findByRole("dialog", { name: "Новая сделка" });
    await user.type(within(createDialog).getByRole("textbox", { name: "Название" }), "Тестовая сделка");
    await user.type(within(createDialog).getByRole("textbox", { name: "Потребность" }), "Тестовая потребность");
    await user.type(within(createDialog).getByRole("spinbutton", { name: "Сумма, ₽" }), "1000");
    await user.click(within(createDialog).getByRole("button", { name: "Создать сделку" }));

    expect(await screen.findByRole("dialog", { name: "Тестовая сделка" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent(/\/deals\?deal=deal-/);
  });

  it("assigns a new employee deal to the current user and keeps protected controls hidden", async () => {
    const user = userEvent.setup();
    const employee = { id: "user-employee", name: "Текущий Сотрудник", initials: "ТС", tone: "violet" as const };
    renderPage("/deals", { currentUser: employee, userRole: "employee" });

    await user.click(screen.getAllByRole("button", { name: "Новая сделка" })[0]);
    const createDialog = await screen.findByRole("dialog", { name: "Новая сделка" });
    await user.type(within(createDialog).getByRole("textbox", { name: "Название" }), "Сделка сотрудника");
    await user.type(within(createDialog).getByRole("textbox", { name: "Потребность" }), "Продление");
    await user.type(within(createDialog).getByRole("spinbutton", { name: "Сумма, ₽" }), "1500");
    await user.click(within(createDialog).getByRole("button", { name: "Создать сделку" }));

    const dealDialog = await screen.findByRole("dialog", { name: "Сделка сотрудника" });
    expect(dealDialog.querySelector(".deal-details__row--owner")).toHaveTextContent(employee.name);
    expect(within(dealDialog).queryByRole("button", { name: "Удалить сделку" })).not.toBeInTheDocument();
    expect(within(dealDialog).queryByRole("button", { name: "Изменить ответственного сделки" })).not.toBeInTheDocument();
  });

  it("exposes the kanban as a horizontally scrollable region with accessible stages", async () => {
    const user = userEvent.setup();
    renderPage();

    const kanban = screen.getByRole("region", { name: "Воронка продаж" });
    expect(kanban).toHaveClass("kanban--single-row", "kanban--mobile-scroll");
    expect(kanban).toHaveAttribute("tabindex", "0");
    expect(document.getElementById(kanban.getAttribute("aria-describedby")!)).toHaveTextContent("Прокручивайте воронку по горизонтали");

    const firstStage = within(kanban).getByRole("region", { name: "Новый лид" });
    const collapse = within(firstStage).getByRole("button", { name: "Свернуть этап Новый лид" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    await user.click(collapse);
    expect(collapse).toHaveAttribute("aria-expanded", "false");
    expect(within(firstStage).getByText("Анна Смирнова")).not.toBeVisible();
  });

  it("должен загружать финальный этап только по кнопке с его точным именем", async () => {
    const user = userEvent.setup();
    const loadStage = vi.fn().mockResolvedValue(undefined);

    render(
      <DndContext>
        <StageColumn
          stage={{ id: "won", name: "Успешно реализовано", color: "green", stageType: "won" }}
          deals={[]}
          selectedDealId={null}
          onSelect={vi.fn()}
          onAdd={vi.fn()}
          deferred
          loadError={null}
          hasMore={false}
          loadingMore={false}
          onLoadDeferred={loadStage}
          onLoadMore={vi.fn()}
        />
      </DndContext>,
    );

    const stage = screen.getByRole("region", { name: "Успешно реализовано" });
    const button = within(stage).getByRole("button", { name: "Загрузить этап «Успешно реализовано»" });
    expect(button).toHaveTextContent(/^Успешно реализовано$/);
    expect(within(stage).queryByRole("button", { name: /Добавить сделку/ })).not.toBeInTheDocument();

    await user.click(button);
    expect(loadStage).toHaveBeenCalledOnce();
  });

  it("блокирует кнопки этапов, пока уже выполняется другой запрос", () => {
    render(
      <DndContext>
        <StageColumn
          stage={{ id: "lost", name: "Закрыто и не реализовано", color: "amber", stageType: "lost" }}
          deals={[]}
          selectedDealId={null}
          onSelect={vi.fn()}
          onAdd={vi.fn()}
          deferred
          loadError={null}
          hasMore={false}
          loadingMore={false}
          requestsBusy
          onLoadDeferred={vi.fn()}
          onLoadMore={vi.fn()}
        />
      </DndContext>,
    );

    expect(screen.getByRole("button", { name: "Загрузить этап «Закрыто и не реализовано»" })).toBeDisabled();
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

    await user.click(within(dialog).getByRole("tab", { name: /Переписка/ }));
    const input = within(dialog).getByPlaceholderText("Написать сообщение");
    await user.type(input, "Предложение готово");
    fireEvent.submit(input.closest("form")!);

    expect(within(dialog).getByText("Предложение готово")).toBeInTheDocument();
  });

  it("separates deal details, custom fields, tasks, messages and history", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    const dialog = await screen.findByRole("dialog", { name: "Кофейня «Слой»" });
    expect(within(dialog).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(expect.arrayContaining([
      "Детали",
      "Поля",
      "Задачи",
      expect.stringMatching(/^Переписка/),
      "История",
    ]));
    expect(within(dialog).getByRole("textbox", { name: "Заметка о сделке" })).toBeInTheDocument();
    expect(within(dialog).queryByPlaceholderText("Написать сообщение")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("tab", { name: "История" }));
    expect(within(dialog).queryByRole("textbox", { name: "Заметка о сделке" })).not.toBeInTheDocument();
    expect(within(dialog).getByText("Сделка создана")).toBeInTheDocument();
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

  it("changes the deal assignee from the deal details", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    const dialog = await screen.findByRole("dialog", { name: "Кофейня «Слой»" });
    const ownerRow = dialog.querySelector(".deal-details__row--owner");
    expect(ownerRow).not.toBeNull();
    expect(within(ownerRow as HTMLElement).getByText("Алексей Кузнецов")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Изменить ответственного сделки" }));
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Ответственный сделки" }), "user-ek");
    await user.click(within(ownerRow as HTMLElement).getByRole("button", { name: "Сохранить ответственного сделки" }));

    expect(within(ownerRow as HTMLElement).getByText("Елена Крылова")).toBeInTheDocument();
    expect(within(ownerRow as HTMLElement).queryByText("Алексей Кузнецов")).not.toBeInTheDocument();
  });

  it("hides destructive, company and assignee mutation controls from employees", async () => {
    renderDrawer({ canAccessCompanies: false, canDelete: false, canManageAssignee: false });

    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    expect(within(dialog).queryByRole("button", { name: "Удалить сделку" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Изменить ответственного сделки" })).not.toBeInTheDocument();
    expect(dialog.querySelector(".deal-details__row--company")).not.toBeInTheDocument();
    expect(dialog.querySelector(".deal-details__row--owner")).toHaveTextContent(initialDeals[0].assignee.name);
  });

  it("requires confirmation before deleting a deal", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByText("Кофейня «Слой»"));
    const dealDialog = await screen.findByRole("dialog", { name: "Кофейня «Слой»" });
    await user.click(within(dealDialog).getByRole("button", { name: "Удалить сделку" }));

    let confirmation = await screen.findByRole("dialog", { name: "Удалить сделку?" });
    expect(confirmation).toHaveTextContent("Это действие нельзя отменить");
    await user.click(within(confirmation).getByRole("button", { name: "Отмена" }));
    expect(screen.queryByRole("dialog", { name: "Удалить сделку?" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Кофейня «Слой»" })).toBeInTheDocument();

    await user.click(within(dealDialog).getByRole("button", { name: "Удалить сделку" }));
    confirmation = await screen.findByRole("dialog", { name: "Удалить сделку?" });
    await user.click(within(confirmation).getByRole("button", { name: "Удалить сделку" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Кофейня «Слой»" })).not.toBeInTheDocument());
    expect(screen.queryByText("Кофейня «Слой»")).not.toBeInTheDocument();
  });

  it("disables conflicting deal controls while a versioned mutation is pending", async () => {
    renderDrawer({ mutationPending: true });
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });

    expect(within(dialog).getByRole("combobox", { name: "Этап сделки" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Удалить сделку" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Изменить ответственного сделки" })).toBeDisabled();
    expect(dialog).toHaveAttribute("aria-busy", "true");
  });

  it("shows a reconciled conflict message when the stage update fails", async () => {
    const user = userEvent.setup();
    const onMove = vi.fn().mockRejectedValue(new ApiError("conflict", 409, { detail: { code: "version_conflict" } }));
    renderDrawer({ onMove });
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });

    const stage = within(dialog).getByRole("combobox", { name: "Этап сделки" });
    const nextStage = demoPipeline.stages.find((item) => item.id !== initialDeals[0].stageId)!;
    await user.selectOptions(stage, nextStage.id);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Данные обновлены — повторите действие");
  });

  it("keeps the specific conflict explanation in the assignee editor", async () => {
    const user = userEvent.setup();
    const onSetAssignee = vi.fn().mockRejectedValue(new ApiError("conflict", 409, { detail: { code: "version_conflict" } }));
    renderDrawer({ onSetAssignee });
    const dialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    await user.click(within(dialog).getByRole("button", { name: "Изменить ответственного сделки" }));
    const assignee = Object.values(demoUsers).find((item) => item.id !== initialDeals[0].assignee.id)!;
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Ответственный сделки" }), assignee.id);
    await user.click(within(dialog).getByRole("button", { name: "Сохранить ответственного сделки" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Данные обновлены — повторите действие");
  });

  it("keeps delete confirmation open and explains a version conflict", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn().mockRejectedValue(new ApiError("conflict", 409, { detail: { code: "version_conflict" } }));
    renderDrawer({ onDelete });
    const dealDialog = await screen.findByRole("dialog", { name: initialDeals[0].title });
    await user.click(within(dealDialog).getByRole("button", { name: "Удалить сделку" }));
    const confirmation = await screen.findByRole("dialog", { name: "Удалить сделку?" });
    await user.click(within(confirmation).getByRole("button", { name: "Удалить сделку" }));

    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Данные обновлены — повторите действие");
    expect(screen.getByRole("dialog", { name: "Удалить сделку?" })).toBeInTheDocument();
  });

  it("makes the mobile and tablet overlay drawer modal while desktop remains non-modal", async () => {
    const user = userEvent.setup();
    const mediaListeners = new Set<() => void>();
    const tabletMatchMedia = vi.fn().mockReturnValue({
      matches: true,
      media: "(max-width: 1100px)",
      onchange: null,
      addEventListener: (_type: string, listener: () => void) => mediaListeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => mediaListeners.delete(listener),
      dispatchEvent: vi.fn(),
    });
    vi.stubGlobal("matchMedia", tabletMatchMedia);

    const mobile = renderDrawer();
    const mobileDrawer = await screen.findByRole("dialog", { name: initialDeals[0].title });
    expect(tabletMatchMedia).toHaveBeenCalledWith("(max-width: 1100px)");
    expect(mobile.container).toHaveAttribute("aria-hidden", "true");
    await user.click(within(mobileDrawer).getByRole("button", { name: "Удалить сделку" }));
    const confirmation = await screen.findByRole("dialog", { name: "Удалить сделку?" });
    expect(confirmation).toContainElement(document.activeElement as HTMLElement);
    expect(screen.queryByRole("dialog", { name: initialDeals[0].title })).not.toBeInTheDocument();
    await user.click(within(confirmation).getByRole("button", { name: "Отмена" }));
    expect(await screen.findByRole("dialog", { name: initialDeals[0].title })).toBeInTheDocument();
    cleanup();

    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
      matches: false,
      media: "(max-width: 1100px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    const desktop = renderDrawer();
    await screen.findByRole("dialog", { name: initialDeals[0].title });
    expect(desktop.container).not.toHaveAttribute("aria-hidden");
  });
});
