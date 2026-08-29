import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiTask, ApiUser, CursorPage } from "../types/api";
import TasksPage from "./TasksPage";

const { getMock, patchMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  api: { get: getMock, patch: patchMock, post: postMock },
  remoteEnabled: true,
}));

vi.mock("../state/crm-store", () => ({
  CrmProvider: ({ children }: PropsWithChildren) => children,
  useCrm: () => ({ deals: [] }),
}));

const assignee: ApiUser = {
  id: "user-1",
  email: "owner@example.com",
  full_name: "Тестовый Пользователь",
  role: "owner",
};

function task(id: string, title: string, status: ApiTask["status"] = "open"): ApiTask {
  return {
    id,
    title,
    description: null,
    task_type: "follow_up",
    status,
    due_at: "2026-08-29T10:00:00Z",
    remind_at: null,
    assignee_id: assignee.id,
    deal_id: null,
    contact_id: null,
    company_id: null,
    completed_at: status === "completed" ? "2026-08-29T11:00:00Z" : null,
    version: 1,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TasksPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  postMock.mockReset();
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
          task("task-1", "Отправить предложение"),
          task("task-completed", "Завершённая задача", "completed"),
        ],
        next_cursor: null,
      };
    } else {
      page = { items: [task("task-1", "Отправить предложение")], next_cursor: "tasks-page-2" };
    }
    return Promise.resolve(page);
  });
});

afterEach(cleanup);

describe("TasksPage", () => {
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
});
