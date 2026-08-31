import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";

import { DealsPage } from "../features/deals/DealsPage";
import { ApiError } from "../lib/api";
import type { ApiDeal, ApiPipeline, ApiTask, ApiUser, CursorPage } from "../types/api";
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
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number, public readonly details?: unknown) {
      super(message);
    }
  },
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
    { id: "stage-service-won", pipeline_id: "pipeline-service", name: "Выполнено", color: "#16a36d", position: 1, stage_type: "won", version: 1 },
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

const owner: ApiUser = {
  id: "user-owner",
  email: "owner@example.com",
  full_name: "Тестовый сотрудник",
  role: "owner",
  version: 1,
};

function StoreProbe() {
  const {
    deals,
    pipeline: activePipeline,
    pipelines,
    loading,
    loadedStageIds,
    nextCursorByStage,
    nextDealSearchCursor,
    loadStageDeals,
    loadMoreDeals,
    loadMoreDealSearch,
    setDealSearch,
    selectPipeline,
    toggleTask,
    dealAssignees,
    setDealAssignee,
    deleteDeal,
    moveDeal,
    selectDeal,
    openDeal,
    selectedDeal,
    selectedDealMutationPending,
  } = useCrm();
  return <div>
    <output aria-label="Статус">{loading ? "loading" : "ready"}</output>
    <output aria-label="Сделки">{deals.map((item) => item.title).join("|")}</output>
    <output aria-label="Версии сделок">{deals.map((item) => `${item.version}:${item.stageId}`).join("|")}</output>
    <output aria-label="Воронки">{pipelines.map((item) => item.id).join("|") || "нет"}</output>
    <output aria-label="Активная воронка">{activePipeline.id}</output>
    <output aria-label="Открытая сделка">{selectedDeal?.title ?? "нет"}</output>
    <output aria-label="Мутация сделки">{selectedDealMutationPending ? "pending" : "idle"}</output>
    <output aria-label="Задачи сделок">{deals.flatMap((item) => item.tasks).map((item) => item.title).join("|")}</output>
    <output aria-label="Состояния задач">{deals.flatMap((item) => item.tasks).map((item) => item.completed ? "done" : "open").join("|")}</output>
    <output aria-label="Ответственные сделок">{deals.map((item) => item.assignee.name).join("|")}</output>
    <output aria-label="Финальный этап">{loadedStageIds["stage-won"] ? "loaded" : "deferred"}</output>
    <button type="button" onClick={() => void loadStageDeals("stage-won")}>Загрузить финал</button>
    {nextCursorByStage["stage-won"] ? <button type="button" onClick={() => void loadMoreDeals("stage-won")}>Ещё финал</button> : null}
    {nextDealSearchCursor ? <button type="button" onClick={() => void loadMoreDealSearch()}>Ещё результаты поиска</button> : null}
    <button type="button" onClick={() => setDealSearch("архив")}>Поиск в архиве</button>
    <button type="button" onClick={() => void selectPipeline("pipeline-service")}>Выбрать сервис</button>
    <button type="button" onClick={() => {
      const dealWithTask = deals.find((item) => item.tasks.length);
      const task = dealWithTask?.tasks[0];
      if (dealWithTask && task) void toggleTask(dealWithTask.id, task.id);
    }}>Завершить задачу сделки</button>
    <button type="button" onClick={() => {
      const currentDeal = deals[0];
      const nextAssignee = dealAssignees[0];
      if (currentDeal && nextAssignee) void setDealAssignee(currentDeal.id, nextAssignee).catch(() => undefined);
    }}>Назначить ответственного</button>
    <button type="button" onClick={() => {
      const currentDeal = deals[0];
      if (currentDeal) void deleteDeal(currentDeal.id).catch(() => undefined);
    }}>Удалить первую сделку</button>
    <button type="button" onClick={() => {
      const currentDeal = deals[0];
      if (currentDeal) selectDeal(currentDeal.id);
    }}>Открыть первую сделку</button>
    <button type="button" onClick={() => void openDeal("deal-deep-link").catch(() => undefined)}>Открыть сделку по ссылке</button>
    <button type="button" onClick={() => {
      const currentDeal = deals[0];
      if (currentDeal) void moveDeal(currentDeal.id, "stage-won").catch(() => undefined);
    }}>Перенести первую сделку</button>
    <button type="button" onClick={() => {
      const currentDeal = deals[0];
      const nextAssignee = dealAssignees[0];
      if (!currentDeal || !nextAssignee) return;
      void setDealAssignee(currentDeal.id, nextAssignee).catch(() => undefined);
      void moveDeal(currentDeal.id, "stage-won").catch(() => undefined);
      void deleteDeal(currentDeal.id).catch(() => undefined);
    }}>Назначить, перенести и удалить одновременно</button>
  </div>;
}

function ClearDealRouteButton() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate("/deals")}>Убрать сделку из адреса</button>;
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Адрес">{`${location.pathname}${location.search}`}</output>;
}

describe("CrmProvider deal stage loading", () => {
  let taskPagePromise: Promise<CursorPage<ApiTask>>;
  let deferredWonPage: Promise<CursorPage<ApiDeal>> | null;

  beforeEach(() => {
    taskPagePromise = Promise.resolve({ items: [wonDealTask], next_cursor: null });
    deferredWonPage = null;
    apiMocks.get.mockReset();
    apiMocks.patch.mockReset();
    apiMocks.delete.mockReset();
    apiMocks.delete.mockResolvedValue(undefined);
    apiMocks.patch.mockResolvedValue({ ...wonDealTask, status: "completed", version: 2 });
    apiMocks.get.mockImplementation((path: string) => {
      if (path === "/pipelines") return Promise.resolve([pipeline, secondaryPipeline]);
      if (path === "/sources") return Promise.resolve([]);
      if (path === "/users") return Promise.resolve([owner]);
      if (path === "/tasks?limit=100") return taskPagePromise;
      if (path === "/deals/deal-open/messages?limit=100") return Promise.resolve({ items: [], next_cursor: null });
      if (path.includes("stage_id=stage-service-open")) {
        return Promise.resolve({ items: [deal("deal-service", "stage-service-open", "Сервисная сделка", secondaryPipeline.id)], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path.includes("stage_id=stage-open")) {
        return Promise.resolve({ items: [deal("deal-open", "stage-open", "Активная сделка")], next_cursor: null } satisfies CursorPage<ApiDeal>);
      }
      if (path === "/deals?limit=100&pipeline_id=pipeline-sales&search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2") {
        return Promise.resolve({ items: [deal("deal-won-1", "stage-won", "Закрытая сделка")], next_cursor: "search-next" } satisfies CursorPage<ApiDeal>);
      }
      if (path === "/deals?limit=100&pipeline_id=pipeline-sales&search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2&cursor=search-next") {
        return Promise.resolve({ items: [deal("deal-lost-search", "stage-lost", "Ещё одна закрытая")], next_cursor: null } satisfies CursorPage<ApiDeal>);
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

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

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
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Закрытая сделка"));
    expect(screen.getByLabelText("Состояния задач")).toHaveTextContent("done");
    expect(apiMocks.get.mock.calls.map(([path]) => String(path))).toContain(
      "/deals?limit=100&pipeline_id=pipeline-sales&search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2",
    );
    expect(apiMocks.get.mock.calls.map(([path]) => String(path)).filter((path) => path.includes("search=%D0%B0%D1%80%D1%85%D0%B8%D0%B2"))).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Ещё результаты поиска" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Ещё одна закрытая"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Закрытая сделка");
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

  it("открывает по deep-link отсутствующую в списке сделку из финального этапа другой воронки", async () => {
    const deepLinkedDeal = deal(
      "deal-deep-link",
      "stage-service-won",
      "Сделка из уведомления",
      secondaryPipeline.id,
    );
    const neverLoadedFinalStage = new Promise<CursorPage<ApiDeal>>(() => undefined);
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => {
      if (path === "/deals/deal-deep-link") return Promise.resolve(deepLinkedDeal);
      if (path === "/deals/deal-deep-link/messages?limit=100") return Promise.resolve({ items: [], next_cursor: null });
      if (path.includes("stage_id=stage-service-won")) return neverLoadedFinalStage;
      return baseGet(path);
    });
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));

    fireEvent.click(screen.getByRole("button", { name: "Открыть сделку по ссылке" }));

    await waitFor(() => expect(apiMocks.get).toHaveBeenCalledWith("/deals/deal-deep-link"));
    await waitFor(() => expect(screen.getByLabelText("Активная воронка")).toHaveTextContent(secondaryPipeline.id));
    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("Сделка из уведомления");
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Сделка из уведомления");
  });

  it("немедленно закрывает выбранную сделку сотрудника при изменении доступа", async () => {
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => {
      if (path === "/deals/deal-open") {
        return Promise.resolve(deal("deal-open", "stage-open", "Активная сделка"));
      }
      if (path.startsWith("/activity?")) return Promise.resolve({ items: [], next_cursor: null });
      if (path.startsWith("/custom-fields?")) return Promise.resolve([]);
      return baseGet(path);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={["/deals"]}>
        <QueryClientProvider client={queryClient}>
          <CrmProvider userRole="employee">
            <DealsPage />
            <LocationProbe />
          </CrmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Активная сделка")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Активная сделка"));
    await waitFor(() => expect(apiMocks.get.mock.calls
      .filter(([path]) => path === "/deals/deal-open")).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Активная сделка" })).toBeInTheDocument());
    expect(screen.getByLabelText("Адрес")).toHaveTextContent("/deals?deal=deal-open");

    act(() => window.dispatchEvent(new Event("pulse:access-changed")));

    expect(screen.queryByRole("dialog", { name: "Активная сделка" })).not.toBeInTheDocument();
    expect(screen.queryByText("Активная сделка")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Адрес")).toHaveTextContent("/deals"));
    expect(apiMocks.get.mock.calls.filter(([path]) => path === "/deals/deal-open")).toHaveLength(1);
  });

  it("инвалидирует незавершённое открытие сделки при изменении доступа", async () => {
    let resolveDeepLink!: (value: ApiDeal) => void;
    const deepLinkResponse = new Promise<ApiDeal>((resolve) => { resolveDeepLink = resolve; });
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => path === "/deals/deal-deep-link"
      ? deepLinkResponse
      : baseGet(path));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={["/deals?deal=deal-deep-link"]}>
        <QueryClientProvider client={queryClient}>
          <CrmProvider userRole="employee">
            <DealsPage />
            <StoreProbe />
            <LocationProbe />
          </CrmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(apiMocks.get.mock.calls
      .some(([path]) => path === "/deals/deal-deep-link")).toBe(true));

    act(() => window.dispatchEvent(new Event("pulse:access-changed")));

    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("нет");
    await waitFor(() => expect(screen.getByLabelText("Адрес")).toHaveTextContent("/deals"));
    await act(async () => resolveDeepLink(deal(
      "deal-deep-link",
      "stage-service-won",
      "Закрытые данные",
      secondaryPipeline.id,
    )));

    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("нет");
    expect(screen.queryByText("Закрытые данные")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Активная воронка")).toHaveTextContent(pipeline.id);
  });

  it("не возвращает задачу из запроса, начатого до изменения доступа", async () => {
    let resolveStaleTasks!: (page: CursorPage<ApiTask>) => void;
    const staleTasks = new Promise<CursorPage<ApiTask>>((resolve) => { resolveStaleTasks = resolve; });
    let taskRequests = 0;
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => {
      if (path === "/tasks?limit=100") {
        taskRequests += 1;
        return taskRequests === 1 ? staleTasks : Promise.reject(new Error("fresh task request failed"));
      }
      return baseGet(path);
    });
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");
    expect(taskRequests).toBe(1);

    act(() => window.dispatchEvent(new Event("pulse:access-changed")));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("");
    await act(async () => resolveStaleTasks({
      items: [{ ...wonDealTask, id: "task-revoked", title: "Отозванная задача", deal_id: "deal-open" }],
      next_cursor: null,
    }));

    act(() => window.dispatchEvent(new Event("pulse:refresh")));
    await waitFor(() => expect(taskRequests).toBe(2));
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");
    expect(screen.getByLabelText("Задачи сделок")).not.toHaveTextContent("Отозванная задача");
  });

  it("не применяет поздний ответ deep-link после удаления deal из URL", async () => {
    let resolveDeepLink!: (value: ApiDeal) => void;
    const deepLinkResponse = new Promise<ApiDeal>((resolve) => { resolveDeepLink = resolve; });
    const deepLinkedDeal = deal(
      "deal-deep-link",
      "stage-service-won",
      "Сделка из отменённой ссылки",
      secondaryPipeline.id,
    );
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => path === "/deals/deal-deep-link"
      ? deepLinkResponse
      : baseGet(path));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={["/deals?deal=deal-deep-link"]}>
        <QueryClientProvider client={queryClient}>
          <CrmProvider>
            <DealsPage />
            <StoreProbe />
            <ClearDealRouteButton />
          </CrmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    await waitFor(() => expect(apiMocks.get.mock.calls
      .some(([path]) => path === "/deals/deal-deep-link")).toBe(true));
    const detailCall = apiMocks.get.mock.calls.find(([path]) => path === "/deals/deal-deep-link");
    const signal = (detailCall?.[1] as RequestInit | undefined)?.signal;
    expect(signal).toBeInstanceOf(AbortSignal);

    fireEvent.click(screen.getByRole("button", { name: "Убрать сделку из адреса" }));
    expect(signal?.aborted).toBe(true);
    await act(async () => resolveDeepLink(deepLinkedDeal));

    expect(screen.getByLabelText("Активная воронка")).toHaveTextContent(pipeline.id);
    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("нет");
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");
    expect(screen.queryByRole("dialog", { name: "Сделка из отменённой ссылки" })).not.toBeInTheDocument();
  });

  it("не теряет обычную выбранную сделку из-за позднего неполного ответа списка", async () => {
    let resolveStaleList!: (page: CursorPage<ApiDeal>) => void;
    const staleList = new Promise<CursorPage<ApiDeal>>((resolve) => { resolveStaleList = resolve; });
    let returnStaleList = false;
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => {
      if (returnStaleList && path.includes("stage_id=stage-open")) return staleList;
      return baseGet(path);
    });
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Открыть первую сделку" }));
    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("Активная сделка");

    returnStaleList = true;
    act(() => window.dispatchEvent(new Event("pulse:refresh")));
    await waitFor(() => expect(apiMocks.get.mock.calls
      .filter(([path]) => String(path).includes("stage_id=stage-open"))).toHaveLength(2));
    await act(async () => resolveStaleList({ items: [], next_cursor: null }));

    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Активная сделка");
    expect(screen.getByLabelText("Открытая сделка")).toHaveTextContent("Активная сделка");
  });

  it("игнорирует поздний ответ финального этапа от предыдущего поиска", async () => {
    let resolveStale!: (page: CursorPage<ApiDeal>) => void;
    deferredWonPage = new Promise((resolve) => { resolveStale = resolve; });
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));

    fireEvent.click(screen.getByRole("button", { name: "Загрузить финал" }));
    fireEvent.click(screen.getByRole("button", { name: "Поиск в архиве" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Закрытая сделка"));

    await act(async () => resolveStale({
      items: [deal("deal-won-stale", "stage-won", "Устаревшая закрытая")],
      next_cursor: null,
    }));
    expect(screen.getByLabelText("Сделки")).not.toHaveTextContent("Устаревшая закрытая");
    expect(screen.getByLabelText("Сделки")).toHaveTextContent("Закрытая сделка");
  });

  it("сбрасывает старый cursor сразу после изменения поиска", async () => {
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Загрузить финал" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ещё финал" })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Поиск в архиве" }));
    expect(screen.queryByRole("button", { name: "Ещё финал" })).not.toBeInTheDocument();
  });

  it("назначает ответственного и удаляет сделку с актуальной версией", async () => {
    const updated = { ...deal("deal-open", "stage-open", "Активная сделка"), assignee_id: owner.id, version: 2 };
    apiMocks.patch.mockImplementation((path: string) => path === "/deals/deal-open"
      ? Promise.resolve(updated)
      : Promise.resolve({ ...wonDealTask, status: "completed", version: 2 }));
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));

    fireEvent.click(screen.getByRole("button", { name: "Назначить ответственного" }));
    await waitFor(() => expect(screen.getByLabelText("Ответственные сделок")).toHaveTextContent(owner.full_name));
    expect(apiMocks.patch).toHaveBeenCalledWith("/deals/deal-open", {
      expected_version: 1,
      assignee_id: owner.id,
    });

    fireEvent.click(screen.getByRole("button", { name: "Удалить первую сделку" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).not.toHaveTextContent("Активная сделка"));
    expect(apiMocks.delete).toHaveBeenCalledWith("/deals/deal-open?expected_version=2");
  });

  it("не раскрывает demo-воронку до завершения remote metadata bootstrap", async () => {
    let resolvePipelines!: (value: ApiPipeline[]) => void;
    const pipelinesPromise = new Promise<ApiPipeline[]>((resolve) => { resolvePipelines = resolve; });
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => path === "/pipelines" ? pipelinesPromise : baseGet(path));

    render(<CrmProvider><StoreProbe /></CrmProvider>);
    expect(screen.getByLabelText("Воронки")).toHaveTextContent("нет");

    await act(async () => resolvePipelines([pipeline, secondaryPipeline]));
    await waitFor(() => expect(screen.getByLabelText("Воронки")).toHaveTextContent("pipeline-sales|pipeline-service"));
  });

  it("блокирует параллельное удаление, пока сохраняется ответственный", async () => {
    let resolveAssignment!: (value: ApiDeal) => void;
    const assignmentPromise = new Promise<ApiDeal>((resolve) => { resolveAssignment = resolve; });
    apiMocks.patch.mockImplementation((path: string) => path === "/deals/deal-open"
      ? assignmentPromise
      : Promise.resolve({ ...wonDealTask, status: "completed", version: 2 }));

    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Открыть первую сделку" }));
    fireEvent.click(screen.getByRole("button", { name: "Назначить, перенести и удалить одновременно" }));

    await waitFor(() => expect(screen.getByLabelText("Мутация сделки")).toHaveTextContent("pending"));
    expect(apiMocks.delete).not.toHaveBeenCalled();
    expect(apiMocks.patch.mock.calls.some(([path]) => path === "/deals/deal-open/stage")).toBe(false);
    await act(async () => resolveAssignment({
      ...deal("deal-open", "stage-open", "Активная сделка"),
      assignee_id: owner.id,
      version: 2,
    }));
    await waitFor(() => expect(screen.getByLabelText("Мутация сделки")).toHaveTextContent("idle"));

    fireEvent.click(screen.getByRole("button", { name: "Удалить первую сделку" }));
    await waitFor(() => expect(apiMocks.delete).toHaveBeenCalledWith("/deals/deal-open?expected_version=2"));
  });

  it("после 409 перечитывает сделку и повторяет мутацию с новой версией", async () => {
    const fresh = { ...deal("deal-open", "stage-open", "Актуальная сделка"), version: 4 };
    const baseGet = apiMocks.get.getMockImplementation()!;
    apiMocks.get.mockImplementation((path: string) => path === "/deals/deal-open" ? Promise.resolve(fresh) : baseGet(path));
    apiMocks.patch.mockRejectedValueOnce(new ApiError("conflict", 409, { detail: { code: "version_conflict" } }));

    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Назначить ответственного" }));

    await waitFor(() => expect(apiMocks.get).toHaveBeenCalledWith("/deals/deal-open"));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).toHaveTextContent("Актуальная сделка"));
    expect(screen.getByLabelText("Версии сделок")).toHaveTextContent("4:stage-open");

    apiMocks.patch.mockResolvedValue({ ...fresh, assignee_id: owner.id, version: 5 });
    fireEvent.click(screen.getByRole("button", { name: "Назначить ответственного" }));
    await waitFor(() => expect(screen.getByLabelText("Ответственные сделок")).toHaveTextContent(owner.full_name));
    expect(apiMocks.patch).toHaveBeenLastCalledWith("/deals/deal-open", {
      expected_version: 4,
      assignee_id: owner.id,
    });
  });

  it("считает 404 при удалении идемпотентным и закрывает сделку локально", async () => {
    apiMocks.delete.mockRejectedValueOnce(new ApiError("not found", 404));
    render(<CrmProvider><StoreProbe /></CrmProvider>);
    await waitFor(() => expect(screen.getByLabelText("Статус")).toHaveTextContent("ready"));

    fireEvent.click(screen.getByRole("button", { name: "Удалить первую сделку" }));
    await waitFor(() => expect(screen.getByLabelText("Сделки")).not.toHaveTextContent("Активная сделка"));
    expect(apiMocks.get).not.toHaveBeenCalledWith("/deals/deal-open");
  });
});
