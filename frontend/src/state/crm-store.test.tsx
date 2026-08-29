import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiDeal, ApiPipeline, ApiTask, CursorPage } from "../types/api";
import { CrmProvider, useCrm } from "./crm-store";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  upload: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  remoteEnabled: true,
  ApiError: class ApiError extends Error {},
  api: { ...apiMocks, setCsrf: vi.fn() },
}));

const pipeline: ApiPipeline = {
  id: "pipeline-sales",
  name: "Продажи",
  position: 0,
  is_active: true,
  version: 1,
  stages: [
    { id: "stage-open", pipeline_id: "pipeline-sales", name: "Новый лид", color: "#4b96f8", position: 0, stage_type: "open", version: 1 },
    { id: "stage-won", pipeline_id: "pipeline-sales", name: "Успешно реализовано", color: "#16a36d", position: 1, stage_type: "won", version: 1 },
    { id: "stage-lost", pipeline_id: "pipeline-sales", name: "Закрыто и не реализовано", color: "#929aaa", position: 2, stage_type: "lost", version: 1 },
  ],
};

const secondaryPipeline: ApiPipeline = {
  id: "pipeline-service",
  name: "Сервис",
  position: 1,
  is_active: true,
  version: 1,
  stages: [
    { id: "stage-service-open", pipeline_id: "pipeline-service", name: "В работе", color: "#4b96f8", position: 0, stage_type: "open", version: 1 },
  ],
};

function deal(id: string, stageId: string, title: string, pipelineId = pipeline.id): ApiDeal {
  return {
    id,
    title,
    pipeline_id: pipelineId,
    stage_id: stageId,
    company_id: null,
    company: null,
    contact_ids: [],
    primary_contact: null,
    assignee_id: null,
    source_id: null,
    amount: 10_000,
    currency: "RUB",
    tags: [],
    custom_fields: {},
    next_purchase_at: null,
    last_activity_at: "2026-08-29T10:00:00Z",
    version: 1,
    created_at: "2026-08-29T10:00:00Z",
    updated_at: "2026-08-29T10:00:00Z",
  };
}

const wonDealTask: ApiTask = {
  id: "task-won",
  title: "Проверить закрытую сделку",
  description: null,
  task_type: "follow_up",
  status: "open",
  due_at: "2026-08-30T10:00:00Z",
  remind_at: null,
  assignee_id: "user-owner",
  deal_id: "deal-won-1",
  contact_id: null,
  company_id: null,
  completed_at: null,
  version: 1,
};

function StoreProbe() {
  const {
    deals,
    loading,
    loadedStageIds,
    nextCursorByStage,
    loadStageDeals,
    loadMoreDeals,
    setDealSearch,
    selectPipeline,
    toggleTask,
  } = useCrm();
  return <div>
    <output aria-label="Статус">{loading ? "loading" : "ready"}</output>
    <output aria-label="Сделки">{deals.map((item) => item.title).join("|")}</output>
    <output aria-label="Задачи сделок">{deals.flatMap((item) => item.tasks).map((item) => item.title).join("|")}</output>
    <output aria-label="Состояния задач">{deals.flatMap((item) => item.tasks).map((item) => item.completed ? "done" : "open").join("|")}</output>
    <output aria-label="Финальный этап">{loadedStageIds["stage-won"] ? "loaded" : "deferred"}</output>
    <button type="button" onClick={() => void loadStageDeals("stage-won")}>Загрузить финал</button>
    {nextCursorByStage["stage-won"] ? <button type="button" onClick={() => void loadMoreDeals("stage-won")}>Ещё финал</button> : null}
    <button type="button" onClick={() => setDealSearch("архив")}>Поиск в архиве</button>
    <button type="button" onClick={() => void selectPipeline("pipeline-service")}>Выбрать сервис</button>
    <button type="button" onClick={() => {
      const dealWithTask = deals.find((item) => item.tasks.length);
      const task = dealWithTask?.tasks[0];
      if (dealWithTask && task) void toggleTask(dealWithTask.id, task.id);
    }}>Завершить задачу сделки</button>
  </div>;
}

describe("CrmProvider deal stage loading", () => {
  let taskPagePromise: Promise<CursorPage<ApiTask>>;
  let deferredWonPage: Promise<CursorPage<ApiDeal>> | null;

  beforeEach(() => {
    taskPagePromise = Promise.resolve({ items: [wonDealTask], next_cursor: null });
    deferredWonPage = null;
    apiMocks.get.mockReset();
    apiMocks.patch.mockReset();
    apiMocks.patch.mockResolvedValue({ ...wonDealTask, status: "completed", version: 2 });
    apiMocks.get.mockImplementation((path: string) => {
      if (path === "/pipelines") return Promise.resolve([pipeline, secondaryPipeline]);
      if (path === "/sources" || path === "/users") return Promise.resolve([]);
      if (path === "/tasks?limit=100") return taskPagePromise;
      if (path.includes("stage_id=stage-service-open")) {
        return Promise.resolve({ items: [deal("deal-service", "stage-service-open", "Сервисная сделка", secondaryPipeline.id)], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path.includes("stage_id=stage-open")) {
        return Promise.resolve({ items: [deal("deal-open", "stage-open", "Активная сделка")], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path.includes("stage_id=stage-won") && path.includes("search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2")) {
        return Promise.resolve({ items: [deal("deal-won-1", "stage-won", "Найдено в архиве")], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path.includes("stage_id=stage-won") && path.includes("cursor=won-next")) {
        return Promise.resolve({ items: [deal("deal-won-2", "stage-won", "Вторая закрытая")], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path.includes("stage_id=stage-won")) {
        return deferredWonPage ?? Promise.resolve({ items: [deal("deal-won-1", "stage-won", "Первая закрытая")], next_cursor: "won-next" } satisfies CursorPage<ApiDeal>);
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
  });

  afterEach(cleanup);

  it("не запрашивает won/lost при старте, но загружает и пагинирует выбранный финальный этап", async () => {
    render(<CrmProvider><StoreProbe /></CrmProvider>);

    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");
    expect(screen.getByLabelText("Финальный этап")).toHaveTextContent("deferred");
    const initialDealRequests = apiMocks.get.mock.calls.map(([path]) => String(path)).filter((path) => path.startsWith("/deals?"));
    expect(initialDealRequests).toHaveLength(1);
    expect(initialDealRequests[0]).toContain("stage_id=stage-open");
    expect(initialDealRequests[0]).not.toContain("stage-won");
    expect(initialDealRequests[0]).not.toContain("stage-lost");

    fireEvent.click(screen.getByRole("button", { name: "Загрузить финал" }));
    await waitFor(() => expect(screen.getByLabelText("Финальный этап")).toHaveTextContent("loaded"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Первая закрытая");
    expect(screen.getByLabelText("Задачи сделок")).toHaveTextContent("Проверить закрытую сделку");
    expect(screen.getByLabelText("Состояния задач")).toHaveTextContent("open");

    fireEvent.click(screen.getByRole("button", { name: "Завершить задачу сделки" }));
    await waitFor(() => expect(screen.getByLabelText("Состояния задач")).toHaveTextContent("done"));

    fireEvent.click(screen.getByRole("button", { name: "Ещё финал" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Вторая закрытая"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Первая закрытая");

    fireEvent.click(screen.getByRole("button", { name: "Поиск в архиве" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Найдено в архиве"));
    expect(screen.getByLabelText("Состояния задач")).toHaveTextContent("done");
    expect(apiMocks.get.mock.calls.map(([path]) => String(path))).toContain(
      "/deals?limit=100&pipeline_id=pipeline-sales&stage_id=stage-won&search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2",
    );
  });

  it("переключает воронку без повторной загрузки метаданных и задач", async () => {
    let resolveTasks!: (page: CursorPage<ApiTask>) => void;
    taskPagePromise = new Promise((resolve) => { resolveTasks = resolve; });
    render(<CrmProvider><StoreProbe /></CrmProvider>);

    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");

    fireEvent.click(screen.getByRole("button", { name: "Выбрать сервис" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Сервисная сделка"));

    const paths = apiMocks.get.mock.calls.map(([path]) => String(path));
    expect(paths.filter((path) => path === "/pipelines")).toHaveLength(1);
    expect(paths.filter((path) => path === "/sources")).toHaveLength(1);
    expect(paths.filter((path) => path === "/users")).toHaveLength(1);
    expect(paths.filter((path) => path === "/tasks?limit=100")).toHaveLength(1);
    await act(async () => resolveTasks({ items: [], next_cursor: null }));
  });

  it("игнорирует поздний ответ финального этапа от предыдущего поиска", async () => {
    let resolveStale!: (page: CursorPage<ApiDeal>) => void;
    deferredWonPage = new Promise((resolve) => { resolveStale = resolve; });
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));

    fireEvent.click(screen.getByRole("button", { name: "Загрузить финал" }));
    fireEvent.click(screen.getByRole("button", { name: "Поиск в архиве" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Найдено в архиве"));

    await act(async () => resolveStale({
      items: [deal("deal-won-stale", "stage-won", "Устаревшая закрытая")],
      next_cursor: null,
    }));
    expect(screen.getByLabelText("Сделки")).not.toHaveTextContent("Устаревшая закрытая");
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Найдено в архиве");
  });

  it("сбрасывает старый cursor сразу после изменения поиска", async () => {
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Загрузить финал" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ещё финал" })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Поиск в архиве" }));
    expect(screen.queryByRole("button", { name: "Ещё финал" })).not.toBeInTheDocument();
  });
});
