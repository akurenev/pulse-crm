import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PropsWithChildren } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiTask, ApiUser, CursorPage } from "../types/api";
import TasksPage from "./TasksPage";

const { deleteMock, getMock, patchMock, postMock } = vi.hoisted(() => ({
  deleteMock: vi.fn(),
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: { delete: deleteMock, get: getMock, patch: patchMock, post: postMock },
  remoteEnabled: true,
}));

vi.mock("../state/crm-store", () => ({
  CrmProvider: ({ children }: PropsWithChildren) => children,
  useCrm: () => ({ deals: [{ id: "deal-1", title: "Тестовая сделка" }] }),
}));

const assignee: ApiUser = {
  id: "user-1",
  email: "owner@example.com",
  full_name: "Тестовый Пользователь",
  role: "owner",
};

function task(
  id: string,
  title: string,
  status: ApiTask["status"] = "open",
  dealId: string | null = null,
  overrides: Partial<ApiTask> = {},
): ApiTask {
  return {
    id,
    title,
    description: null,
    task_type: "follow_up",
    status,
    due_at: "2026-08-29T10:00:00Z",
    remind_at: null,
    assignee_id: assignee.id,
    deal_id: dealId,
    contact_id: null,
    company_id: null,
    completed_at: status === "completed" ? "2026-08-29T11:00:00Z" : null,
    version: 1,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Текущий адрес">{`${location.pathname}${location.search}`}</output>;
}

function renderPage(initialEntry = "/tasks") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <TasksPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...rendered, queryClient };
}

let deletedTaskIds = new Set<string>();

beforeEach(() => {
  deletedTaskIds = new Set<string>();
  deleteMock.mockReset();
  getMock.mockReset();
  patchMock.mockReset();
  postMock.mockReset();
  deleteMock.mockImplementation((path: string) => {
    const taskId = path.match(/^\/tasks\/([^?]+)/)?.[1];
    if (taskId) deletedTaskIds.add(taskId);
    return Promise.resolve();
  });
  getMock.mockImplementation((path: string) => {
    if (path === "/users") return Promise.resolve([assignee]);

    const [pathname, query = ""] = path.split("?");
    if (pathname !== "/tasks") return Promise.reject(new Error(`Unexpected GET ${path}`));
    const params = new URLSearchParams(query);
    const cursor = params.get("cursor");
    const scope = params.get("scope");
    const includeCompleted = params.get("include_completed") === "true";
    let page: CursorPage<ApiTask>;

    if (scope === "overdue") {
      page = {
        items: [
          task("task-overdue", "Позвонить по повторному заказу"),
          ...(includeCompleted ? [task("task-overdue-completed", "Завершённый звонок", "completed")] : []),
        ],
        next_cursor: null,
      };
    } else if (cursor === "tasks-page-2") {
      page = { items: [task("task-2", "Вторая страница")], next_cursor: null };
    } else if (includeCompleted) {
      page = {
        items: [
          task("task-1", "Отправить предложение", "open", "deal-1"),
          task("task-completed", "Завершённая задача", "completed"),
        ],
        next_cursor: null,
      };
    } else {
      page = { items: [task("task-1", "Отправить предложение", "open", "deal-1")], next_cursor: "tasks-page-2" };
    }
    return Promise.resolve({
      ...page,
      items: page.items.filter((item) => !deletedTaskIds.has(item.id)),
    });
  });
});

afterEach(cleanup);

describe("TasksPage", () => {
  it("loads and opens a task outside the current page from a deep-link URL", async () => {
    const user = userEvent.setup();
    const deepLinkedTask = task("task-deep-link", "Задача из уведомления", "open", "deal-1");
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([assignee]);
      if (path === "/tasks/task-deep-link") return Promise.resolve(deepLinkedTask);
      if (path.startsWith("/tasks?")) {
        return Promise.resolve({ items: [task("task-1", "Отправить предложение", "open", "deal-1")], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/tasks?task=task-deep-link&view=compact");

    const dialog = await screen.findByRole("dialog", { name: "Редактировать задачу" });
    expect(within(dialog).getByRole("textbox", { name: "Название" })).toHaveValue("Задача из уведомления");
    expect(getMock).toHaveBeenCalledWith("/tasks/task-deep-link");

    await user.click(within(dialog).getByRole("button", { name: "Закрыть" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Редактировать задачу" })).not.toBeInTheDocument());
    expect(screen.getByRole("status", { name: "Текущий адрес" })).toHaveTextContent("/tasks?view=compact");
  });

  it("keeps the desktop segments and mobile picker in sync", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Отправить предложение")).toBeInTheDocument();
    const mobileFilter = screen.getByRole("combobox", { name: "Статус задач на мобильном" });
    await user.selectOptions(mobileFilter, "overdue");

    expect(await screen.findByText("Позвонить по повторному заказу")).toBeInTheDocument();
    expect(screen.queryByText("Отправить предложение")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Просрочено" })).toHaveAttribute("aria-pressed", "true");
    expect(getMock).toHaveBeenCalledWith("/tasks?limit=25&scope=overdue");

    await user.click(screen.getByRole("button", { name: "Показать закрытые" }));
    expect(await screen.findByText("Завершённый звонок")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/tasks?limit=25&scope=overdue&include_completed=true");

    await user.type(screen.getByPlaceholderText("Поиск по задачам"), "звонок");
    await waitFor(() => {
      expect(getMock).toHaveBeenCalledWith(
        `/tasks?limit=25&scope=overdue&include_completed=true&search=${encodeURIComponent("звонок")}`,
      );
    });
  });

  it("paginates by cursor and resets to the first page when completed tasks are shown", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Отправить предложение")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/tasks?limit=25");
    await user.click(screen.getByRole("button", { name: "Следующая страница" }));

    expect(await screen.findByText("Вторая страница")).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/tasks?limit=25&cursor=tasks-page-2");

    await user.click(screen.getByRole("button", { name: "Показать закрытые" }));

    expect(await screen.findByText("Завершённая задача")).toBeInTheDocument();
    expect(screen.getByText(/Страница 1 ·/)).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/tasks?limit=25&include_completed=true");
    expect(screen.getByRole("button", { name: "Скрыть закрытые" })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Скрыть закрытые" }));
    await waitFor(() => expect(screen.queryByText("Завершённая задача")).not.toBeInTheDocument());
  });

  it("opens the creation dialog from the mobile floating action", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("Отправить предложение");
    await user.click(screen.getByRole("button", { name: "Добавить задачу" }));

    expect(await screen.findByRole("dialog", { name: "Новая задача" })).toBeInTheDocument();
  });

  it("edits a task and refreshes task consumers", async () => {
    const user = userEvent.setup();
    const refreshListener = vi.fn();
    window.addEventListener("pulse:tasks-refresh", refreshListener);
    renderPage();

    await screen.findByText("Отправить предложение");
    await user.click(screen.getByRole("button", { name: "Редактировать задачу «Отправить предложение»" }));

    const dialog = await screen.findByRole("dialog", { name: "Редактировать задачу" });
    const title = within(dialog).getByRole("textbox", { name: "Название" });
    const description = within(dialog).getByRole("textbox", { name: "Описание" });
    expect(within(dialog).getByRole("combobox", { name: "Сделка" })).toHaveValue("deal-1");
    await user.clear(title);
    await user.type(title, "Подготовить новое предложение");
    await user.type(description, "Уточнить условия доставки");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Тип" }), "meeting");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(patchMock).toHaveBeenCalledWith(
      "/tasks/task-1",
      expect.objectContaining({
        expected_version: 1,
        title: "Подготовить новое предложение",
        description: "Уточнить условия доставки",
        task_type: "meeting",
        assignee_id: assignee.id,
        deal_id: "deal-1",
      }),
    ));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Редактировать задачу" })).not.toBeInTheDocument());
    expect(refreshListener).toHaveBeenCalledTimes(1);
    window.removeEventListener("pulse:tasks-refresh", refreshListener);
  });

  it("keeps the opened version, unloaded deal and imported task type during editing", async () => {
    const user = userEvent.setup();
    let storedTask = task(
      "task-snapshot",
      "Автоматическая задача",
      "open",
      "deal-outside-current-pipeline",
      { task_type: "next_purchase" },
    );
    getMock.mockImplementation((path: string) => {
      if (path === "/users") return Promise.resolve([assignee]);
      if (path.startsWith("/tasks?")) {
        return Promise.resolve({ items: [storedTask], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    const { queryClient } = renderPage();

    await screen.findByText("Автоматическая задача");
    await user.click(screen.getByRole("button", { name: "Редактировать задачу «Автоматическая задача»" }));
    const dialog = await screen.findByRole("dialog", { name: "Редактировать задачу" });
    expect(within(dialog).getByRole("combobox", { name: "Тип" })).toHaveValue("next_purchase");
    expect(within(dialog).getByRole("combobox", { name: "Сделка" })).toHaveValue("deal-outside-current-pipeline");

    storedTask = { ...storedTask, title: "Изменено параллельно", version: 2 };
    await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    expect(await screen.findByText("Изменено параллельно")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Название" })).toHaveValue("Автоматическая задача");

    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith(
      "/tasks/task-snapshot",
      expect.objectContaining({
        expected_version: 1,
        title: "Автоматическая задача",
        task_type: "next_purchase",
        deal_id: "deal-outside-current-pipeline",
      }),
    ));
  });

  it("requires confirmation before deleting and removes the task from the query", async () => {
    const user = userEvent.setup();
    const refreshListener = vi.fn();
    window.addEventListener("pulse:tasks-refresh", refreshListener);
    renderPage();

    await screen.findByText("Отправить предложение");
    await user.click(screen.getByRole("button", { name: "Удалить задачу «Отправить предложение»" }));

    let dialog = await screen.findByRole("dialog", { name: "Удалить задачу?" });
    expect(within(dialog).getByText(/без возможности восстановления/)).toBeInTheDocument();
    expect(deleteMock).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Удалить задачу?" })).not.toBeInTheDocument());
    expect(deleteMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Удалить задачу «Отправить предложение»" }));
    dialog = await screen.findByRole("dialog", { name: "Удалить задачу?" });
    await user.click(within(dialog).getByRole("button", { name: "Удалить задачу" }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("/tasks/task-1?expected_version=1"));
    await waitFor(() => expect(screen.queryByText("Отправить предложение")).not.toBeInTheDocument());
    expect(refreshListener).toHaveBeenCalledTimes(1);
    window.removeEventListener("pulse:tasks-refresh", refreshListener);
  });
});
