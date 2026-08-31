/* eslint-disable react-refresh/only-export-components */

import {
  createContext,
  startTransition,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PropsWithChildren,
} from "react";

import { initialDeals, pipeline as demoPipeline, users as demoUsers } from "../data/demo";
import { ApiError, api, remoteEnabled } from "../lib/api";
import { normalizeDealTags } from "../lib/deal-tags";
import type { ApiDeal, ApiMessage, ApiMessageWithAttachment, ApiPipeline, ApiSource, ApiTask, ApiUser, CursorPage } from "../types/api";
import type { Deal, Message, Pipeline, SourceCode, Stage, UserSummary } from "../types/crm";

interface NewDealInput {
  title: string;
  subtitle: string;
  amount: number;
  source: SourceCode;
}

interface CrmStore {
  currentUser: UserSummary;
  isEmployee: boolean;
  deals: Deal[];
  pipeline: Pipeline;
  pipelines: Pipeline[];
  loading: boolean;
  error: string | null;
  selectedDealId: string | null;
  selectedDeal: Deal | null;
  selectedDealMutationPending: boolean;
  dealAssignees: UserSummary[];
  selectDeal: (dealId: string | null) => void;
  openDeal: (dealId: string, signal?: AbortSignal) => Promise<void>;
  selectPipeline: (pipelineId: string) => Promise<void>;
  moveDeal: (dealId: string, stageId: string) => Promise<void>;
  setNextPurchase: (dealId: string, date: string | null) => Promise<void>;
  setDealContact: (dealId: string, contact: { id: string; name: string; phone?: string; email?: string } | null) => Promise<void>;
  setDealCompany: (dealId: string, company: { id: string; name: string } | null) => Promise<void>;
  setDealAssignee: (dealId: string, assignee: UserSummary | null) => Promise<void>;
  setDealTags: (dealId: string, tags: string[]) => Promise<void>;
  setDealSearch: (query: string) => void;
  setDealCustomFields: (dealId: string, fields: Record<string, unknown>) => Promise<void>;
  nextCursorByStage: Record<string, string | null>;
  nextDealSearchCursor: string | null;
  loadedStageIds: Record<string, boolean>;
  stageLoadErrorByStage: Record<string, string | null>;
  loadingStageId: string | null;
  loadingMoreDealSearch: boolean;
  loadStageDeals: (stageId: string) => Promise<void>;
  loadMoreDeals: (stageId: string) => Promise<void>;
  loadMoreDealSearch: () => Promise<void>;
  addDeal: (input: NewDealInput) => Promise<Deal>;
  deleteDeal: (dealId: string) => Promise<void>;
  sendMessage: (dealId: string, body: string, attachment?: File) => Promise<void>;
  retryMessage: (dealId: string, messageId: string) => Promise<void>;
  toggleTask: (dealId: string, taskId: string) => Promise<void>;
}

const CrmContext = createContext<CrmStore | null>(null);

export class DealMutationInProgressError extends Error {
  constructor() {
    super("Дождитесь завершения текущего изменения сделки");
    this.name = "DealMutationInProgressError";
  }
}

const sourceCodes = new Set<SourceCode>([
  "manual", "email", "telegram", "max", "webhook", "html_form", "amo_import",
]);
const tones: UserSummary["tone"][] = ["blue", "violet", "green", "amber"];

function initials(name: string): string {
  return name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru");
}

function userSummary(user: ApiUser | undefined, fallbackId = "unassigned"): UserSummary {
  const name = user?.full_name ?? "Не назначен";
  const id = user?.id ?? fallbackId;
  const tone = tones[[...id].reduce((sum, char) => sum + char.charCodeAt(0), 0) % tones.length];
  return { id, name, initials: initials(name) || "—", tone };
}

function stageColor(hex: string, position: number): Stage["color"] {
  const normalized = hex.toLocaleLowerCase();
  if (normalized.includes("22c55e") || normalized.includes("10b981")) return "green";
  if (normalized.includes("f59e0b") || normalized.includes("fbbf24")) return "amber";
  if (normalized.includes("8b5cf6") || normalized.includes("7c3aed")) return "violet";
  return (["blue", "violet", "green", "amber"] as const)[position % 4];
}

function mapPipeline(value: ApiPipeline): Pipeline {
  return {
    id: value.id,
    name: value.name,
    stages: [...value.stages].sort((a, b) => a.position - b.position).map((stage) => ({
      id: stage.id,
      name: stage.name,
      color: stageColor(stage.color, stage.position),
      stageType: stage.stage_type,
    })),
  };
}

function stringField(fields: Record<string, unknown>, key: string): string | undefined {
  const value = fields[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function mapRemoteDeal(
  deal: ApiDeal,
  stages: Stage[],
  loadedUsers: ApiUser[],
  loadedSources: ApiSource[],
  tasks: ApiTask[] = [],
): Deal {
  const usersById = new Map(loadedUsers.map((user) => [user.id, user]));
  const sourcesById = new Map(loadedSources.map((source) => [source.id, source]));
  const source = deal.source_id ? sourcesById.get(deal.source_id) : undefined;
  const code = source && sourceCodes.has(source.key as SourceCode)
    ? source.key as SourceCode
    : "manual";
  const stage = stages.find((item) => item.id === deal.stage_id);
  const customFields = deal.custom_fields;
  const primaryContact = deal.primary_contact;
  const primaryContactName = primaryContact
    ? `${primaryContact.first_name} ${primaryContact.last_name}`.trim()
    : undefined;
  return {
    id: deal.id,
    title: deal.title,
    subtitle: stringField(customFields, "subtitle") ?? "Без описания",
    amount: Number(deal.amount ?? 0),
    currency: "RUB",
    source: code,
    sourceLabel: source?.name ?? "Вручную",
    assignee: userSummary(deal.assignee_id ? usersById.get(deal.assignee_id) : undefined),
    dueDate: stringField(customFields, "due_date") ?? deal.updated_at.slice(0, 10),
    stageId: deal.stage_id,
    status: stage?.stageType ?? "open",
    contactIds: deal.contact_ids,
    contactName: primaryContactName ?? stringField(customFields, "contact_name"),
    phone: primaryContact?.primary_phone ?? primaryContact?.phones[0] ?? stringField(customFields, "phone"),
    email: primaryContact?.primary_email ?? primaryContact?.emails[0] ?? stringField(customFields, "email"),
    companyId: deal.company_id ?? undefined,
    companyName: deal.company?.name,
    tags: deal.tags,
    customFields,
    nextPurchaseAt: deal.next_purchase_at ?? undefined,
    version: deal.version,
    messages: [],
    tasks: tasks.map((task) => ({
      id: task.id,
      title: task.title,
      dueAt: task.due_at,
      completed: task.status === "completed",
      assignee: userSummary(usersById.get(task.assignee_id), task.assignee_id),
      version: task.version,
    })),
  };
}

function mapRemoteTasks(tasks: ApiTask[], loadedUsers: ApiUser[]): Deal["tasks"] {
  const usersById = new Map(loadedUsers.map((user) => [user.id, user]));
  return tasks.map((task) => ({
    id: task.id,
    title: task.title,
    dueAt: task.due_at,
    completed: task.status === "completed",
    assignee: userSummary(usersById.get(task.assignee_id), task.assignee_id),
    version: task.version,
  }));
}

interface CrmProviderProps extends PropsWithChildren {
  currentUser?: UserSummary;
  userRole?: ApiUser["role"];
}

export function CrmProvider({ children, currentUser = demoUsers.ak, userRole = "owner" }: CrmProviderProps) {
  const isEmployee = userRole === "employee";
  const [deals, setDeals] = useState<Deal[]>(() => remoteEnabled ? [] : initialDeals);
  const [pipeline, setPipeline] = useState<Pipeline>(demoPipeline);
  const [pipelines, setPipelines] = useState<Pipeline[]>(() => remoteEnabled ? [] : [demoPipeline]);
  const [loading, setLoading] = useState(remoteEnabled);
  const [error, setError] = useState<string | null>(null);
  const [sources, setSources] = useState<ApiSource[]>([]);
  const [users, setUsers] = useState<ApiUser[]>([]);
  const [nextCursorByStage, setNextCursorByStage] = useState<Record<string, string | null>>({});
  const [nextDealSearchCursor, setNextDealSearchCursor] = useState<string | null>(null);
  const [loadedStageIds, setLoadedStageIds] = useState<Record<string, boolean>>(() => remoteEnabled
    ? {}
    : Object.fromEntries(
      demoPipeline.stages
        .filter((stage) => stage.stageType !== "won" && stage.stageType !== "lost")
        .map((stage) => [stage.id, true]),
    ));
  const [stageLoadErrorByStage, setStageLoadErrorByStage] = useState<Record<string, string | null>>({});
  const [loadingStageId, setLoadingStageId] = useState<string | null>(null);
  const [loadingMoreDealSearch, setLoadingMoreDealSearch] = useState(false);
  const [selectedDealId, setSelectedDealId] = useState<string | null>(null);
  const [dealSearch, setDealSearchQuery] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [taskRefreshTick, setTaskRefreshTick] = useState(0);
  const [metadataRevision, setMetadataRevision] = useState(remoteEnabled ? 0 : 1);
  const [pendingDealMutationIds, setPendingDealMutationIds] = useState<ReadonlySet<string>>(() => new Set());
  const hasLoaded = useRef(!remoteEnabled);
  const activePipelineId = useRef(remoteEnabled ? "" : demoPipeline.id);
  const activeDealSearch = useRef("");
  const dealRequestGeneration = useRef(0);
  const taskRequestGeneration = useRef(0);
  const requestedFinalStageIds = useRef(new Set<string>());
  const tasksByDealRef = useRef(new Map<string, ApiTask[]>());
  const dealMutationLocksRef = useRef(new Set<string>());
  const selectedDealIdRef = useRef<string | null>(null);
  const openDealGeneration = useRef(0);

  const runDealMutation = useCallback(async (dealId: string, mutation: () => Promise<void>) => {
    if (dealMutationLocksRef.current.has(dealId)) throw new DealMutationInProgressError();
    dealMutationLocksRef.current.add(dealId);
    setPendingDealMutationIds((current) => new Set(current).add(dealId));
    try {
      await mutation();
    } finally {
      dealMutationLocksRef.current.delete(dealId);
      setPendingDealMutationIds((current) => {
        const next = new Set(current);
        next.delete(dealId);
        return next;
      });
    }
  }, []);

  useEffect(() => {
    if (!remoteEnabled) return;
    const refresh = () => {
      dealRequestGeneration.current += 1;
      setLoadingStageId(null);
      setLoadingMoreDealSearch(false);
      setRefreshTick((value) => value + 1);
    };
    window.addEventListener("pulse:refresh", refresh);
    return () => window.removeEventListener("pulse:refresh", refresh);
  }, []);

  useEffect(() => {
    if (!remoteEnabled) return;
    const purgeRevokedAccess = () => {
      dealRequestGeneration.current += 1;
      taskRequestGeneration.current += 1;
      openDealGeneration.current += 1;
      selectedDealIdRef.current = null;
      tasksByDealRef.current.clear();
      requestedFinalStageIds.current.clear();
      setDeals([]);
      setSelectedDealId(null);
      setNextCursorByStage({});
      setNextDealSearchCursor(null);
      setLoadedStageIds({});
      setStageLoadErrorByStage({});
      setLoadingStageId(null);
      setLoadingMoreDealSearch(false);
      setLoading(true);
    };
    window.addEventListener("pulse:access-changed", purgeRevokedAccess);
    return () => window.removeEventListener("pulse:access-changed", purgeRevokedAccess);
  }, []);

  useEffect(() => {
    if (!remoteEnabled) return;
    const refreshTasks = () => {
      taskRequestGeneration.current += 1;
      setTaskRefreshTick((value) => value + 1);
    };
    window.addEventListener("pulse:tasks-refresh", refreshTasks);
    return () => window.removeEventListener("pulse:tasks-refresh", refreshTasks);
  }, []);

  useEffect(() => {
    if (!remoteEnabled) return;
    let active = true;
    if (!hasLoaded.current) setLoading(true);
    void Promise.all([
      api.get<ApiPipeline[]>("/pipelines"),
      api.get<ApiSource[]>("/sources"),
      api.get<ApiUser[]>("/users"),
    ]).then(([remotePipelines, loadedSources, loadedUsers]) => {
      if (!active) return;
      const mappedPipelines = remotePipelines.map(mapPipeline);
      const mappedPipeline = mappedPipelines.find((item) => item.id === activePipelineId.current)
        ?? mappedPipelines[0];
      if (!mappedPipeline) throw new Error("В рабочем пространстве нет активной воронки");
      activePipelineId.current = mappedPipeline.id;
      setPipelines(mappedPipelines);
      setPipeline(mappedPipeline);
      setSources(loadedSources);
      setUsers(loadedUsers);
      setMetadataRevision((value) => value + 1);
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      console.error("Pulse CRM metadata bootstrap failed", reason);
      setError("Не удалось загрузить настройки CRM");
      if (!hasLoaded.current) {
        hasLoaded.current = true;
        setLoading(false);
      }
    });
    return () => { active = false; };
  }, [refreshTick]);

  useEffect(() => {
    if (!remoteEnabled || metadataRevision === 0) return;
    let active = true;
    const generation = ++dealRequestGeneration.current;
    const requestedPipelineId = pipeline.id;
    const stagesToLoad = pipeline.stages.filter((stage) =>
      stage.stageType === "open" || requestedFinalStageIds.current.has(stage.id),
    );
    if (!hasLoaded.current) setLoading(true);
    const dealPagesRequest = dealSearch
      ? api.get<CursorPage<ApiDeal>>(
        `/deals?limit=100&pipeline_id=${requestedPipelineId}&search=${encodeURIComponent(dealSearch)}`,
      ).then((page) => [page])
      : Promise.all(stagesToLoad.map((stage) =>
        api.get<CursorPage<ApiDeal>>(`/deals?limit=100&pipeline_id=${requestedPipelineId}&stage_id=${stage.id}`),
      ));
    void dealPagesRequest.then((dealPages) => {
      if (!active || generation !== dealRequestGeneration.current || activePipelineId.current !== requestedPipelineId) return;
      const mappedDeals = dealPages.flatMap((page) => page.items)
        .filter((deal) => deal.pipeline_id === requestedPipelineId)
        .map((deal) => mapRemoteDeal(
          deal,
          pipeline.stages,
          users,
          sources,
          tasksByDealRef.current.get(deal.id) ?? [],
        ));
      setNextCursorByStage(dealSearch ? {} : Object.fromEntries(
        stagesToLoad.map((stage, index) => [stage.id, dealPages[index]?.next_cursor ?? null]),
      ));
      setNextDealSearchCursor(dealSearch ? dealPages[0]?.next_cursor ?? null : null);
      setLoadedStageIds(Object.fromEntries(
        (dealSearch ? pipeline.stages : stagesToLoad).map((stage) => [stage.id, true]),
      ));
      setStageLoadErrorByStage({});
      setDeals((current) => {
        const selected = current.find((deal) => deal.id === selectedDealIdRef.current);
        if (!selected) return mappedDeals;
        if (mappedDeals.some((deal) => deal.id === selected.id)) {
          return mappedDeals.map((deal) => deal.id === selected.id
            ? { ...deal, messages: selected.messages, tasks: selected.tasks }
            : deal);
        }
        return pipeline.stages.some((stage) => stage.id === selected.stageId)
          ? [...mappedDeals, selected]
          : mappedDeals;
      });
      setError(null);
    }).catch((reason: unknown) => {
      if (!active || generation !== dealRequestGeneration.current) return;
      console.error("Pulse CRM deals bootstrap failed", reason);
      setError("Не удалось загрузить сделки");
    }).finally(() => {
      if (!active || generation !== dealRequestGeneration.current) return;
      hasLoaded.current = true;
      setLoading(false);
    });
    return () => { active = false; };
  }, [dealSearch, metadataRevision, pipeline, sources, users]);

  useEffect(() => {
    if (!remoteEnabled || metadataRevision === 0) return;
    let active = true;
    const generation = ++taskRequestGeneration.current;
    void api.get<CursorPage<ApiTask>>("/tasks?limit=100")
      .then((taskPage) => {
        if (!active || generation !== taskRequestGeneration.current) return;
        const tasksByDeal = new Map<string, ApiTask[]>();
        for (const task of taskPage.items) {
          if (!task.deal_id) continue;
          tasksByDeal.set(task.deal_id, [...(tasksByDeal.get(task.deal_id) ?? []), task]);
        }
        tasksByDealRef.current = tasksByDeal;
        setDeals((items) => items.map((deal) => ({
          ...deal,
          tasks: mapRemoteTasks(tasksByDeal.get(deal.id) ?? [], users),
        })));
      })
      .catch((reason: unknown) => {
        if (!active || generation !== taskRequestGeneration.current) return;
        console.error("Pulse CRM deal tasks bootstrap failed", reason);
      });
    return () => { active = false; };
  }, [metadataRevision, taskRefreshTick, users]);

  const selectedDeal = useMemo(
    () => deals.find((deal) => deal.id === selectedDealId) ?? null,
    [deals, selectedDealId],
  );
  const selectedDealMutationPending = Boolean(
    selectedDealId && pendingDealMutationIds.has(selectedDealId),
  );
  const dealAssignees = useMemo(
    () => isEmployee
      ? [currentUser]
      : remoteEnabled
        ? users.map((user) => userSummary(user))
        : Object.values(demoUsers),
    [currentUser, isEmployee, users],
  );

  const removeDealLocally = useCallback((dealId: string) => {
    tasksByDealRef.current.delete(dealId);
    setDeals((items) => items.filter((deal) => deal.id !== dealId));
    setSelectedDealId((selected) => {
      if (selected !== dealId) return selected;
      selectedDealIdRef.current = null;
      openDealGeneration.current += 1;
      return null;
    });
  }, []);

  const mergeRemoteDeal = useCallback((persisted: ApiDeal) => {
    setDeals((items) => items.map((current) => {
      if (current.id !== persisted.id) return current;
      const mapped = mapRemoteDeal(
        persisted,
        pipeline.stages,
        users,
        sources,
        tasksByDealRef.current.get(persisted.id) ?? [],
      );
      return { ...mapped, messages: current.messages, tasks: current.tasks };
    }));
  }, [pipeline.stages, sources, users]);

  const reconcileDeal = useCallback(async (dealId: string) => {
    try {
      const persisted = await api.get<ApiDeal>(`/deals/${dealId}`);
      mergeRemoteDeal(persisted);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        removeDealLocally(dealId);
        return;
      }
      window.dispatchEvent(new Event("pulse:refresh"));
    }
  }, [mergeRemoteDeal, removeDealLocally]);

  const recoverVersionedDeal = useCallback(async (dealId: string, reason: unknown) => {
    if (!(reason instanceof ApiError)) return;
    if (reason.status === 404) {
      removeDealLocally(dealId);
      return;
    }
    if (reason.status === 409) await reconcileDeal(dealId);
  }, [reconcileDeal, removeDealLocally]);

  const persistDealPatch = useCallback(async (
    dealId: string,
    expectedVersion: number,
    payload: Record<string, unknown>,
  ) => {
    try {
      const persisted = await api.patch<ApiDeal>(`/deals/${dealId}`, {
        expected_version: expectedVersion,
        ...payload,
      });
      mergeRemoteDeal(persisted);
    } catch (reason) {
      await recoverVersionedDeal(dealId, reason);
      throw reason;
    }
  }, [mergeRemoteDeal, recoverVersionedDeal]);

  useEffect(() => {
    if (!remoteEnabled || !selectedDealId) return;
    let active = true;
    void api.get<CursorPage<ApiMessage>>(`/deals/${selectedDealId}/messages?limit=100`)
      .then((page) => {
        if (!active) return;
        const messages = page.items.map<Message>((message) => ({
          id: message.id,
          body: message.body,
          direction: message.direction,
          createdAt: message.received_at ?? message.sent_at ?? message.created_at,
          status: message.status,
          lastError: message.last_error ?? undefined,
        }));
        setDeals((items) => items.map((deal) => deal.id === selectedDealId
          ? { ...deal, messages }
          : deal));
      })
      .catch((reason: unknown) => {
        console.error("Pulse CRM messages bootstrap failed", reason);
      });
    return () => { active = false; };
  }, [selectedDealId]);

  const selectDeal = useCallback((dealId: string | null) => {
    openDealGeneration.current += 1;
    selectedDealIdRef.current = dealId;
    setSelectedDealId(dealId);
  }, []);

  const setDealSearch = useCallback((query: string) => {
    const normalized = query.trim();
    if (normalized === activeDealSearch.current) return;
    activeDealSearch.current = normalized;
    dealRequestGeneration.current += 1;
    setLoadingStageId(null);
    setLoadingMoreDealSearch(false);
    setNextCursorByStage({});
    setNextDealSearchCursor(null);
    if (remoteEnabled) {
      setError(null);
      setLoading(true);
    }
    setDealSearchQuery(normalized);
  }, []);

  const selectPipeline = useCallback(async (pipelineId: string) => {
    if (pipelineId === activePipelineId.current) return;
    const next = pipelines.find((item) => item.id === pipelineId);
    if (!next) return;
    dealRequestGeneration.current += 1;
    activePipelineId.current = pipelineId;
    requestedFinalStageIds.current.clear();
    setPipeline(next);
    setDeals([]);
    setNextCursorByStage({});
    setNextDealSearchCursor(null);
    setLoadedStageIds({});
    setStageLoadErrorByStage({});
    setLoadingStageId(null);
    setLoadingMoreDealSearch(false);
    selectedDealIdRef.current = null;
    openDealGeneration.current += 1;
    setSelectedDealId(null);
    if (remoteEnabled) {
      setLoading(true);
    }
  }, [pipelines]);

  const openDeal = useCallback(async (dealId: string, signal?: AbortSignal) => {
    if (signal?.aborted) return;
    const generation = ++openDealGeneration.current;
    const existing = deals.find((deal) => deal.id === dealId);
    if (existing && (!remoteEnabled || !isEmployee)) {
      selectedDealIdRef.current = dealId;
      setSelectedDealId(dealId);
      return;
    }
    if (!remoteEnabled) throw new ApiError("Сделка не найдена", 404);

    const path = `/deals/${encodeURIComponent(dealId)}`;
    let persisted: ApiDeal;
    try {
      persisted = signal
        ? await api.get<ApiDeal>(path, { signal })
        : await api.get<ApiDeal>(path);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) removeDealLocally(dealId);
      throw reason;
    }
    if (signal?.aborted || generation !== openDealGeneration.current) return;
    const targetPipeline = pipelines.find((item) => item.id === persisted.pipeline_id);
    if (!targetPipeline) throw new Error("Воронка сделки недоступна");
    const targetStage = targetPipeline.stages.find((stage) => stage.id === persisted.stage_id);
    if (!targetStage) throw new Error("Этап сделки недоступен");
    const mapped = mapRemoteDeal(
      persisted,
      targetPipeline.stages,
      users,
      sources,
      tasksByDealRef.current.get(persisted.id) ?? [],
    );

    if (targetPipeline.id !== activePipelineId.current) {
      dealRequestGeneration.current += 1;
      activePipelineId.current = targetPipeline.id;
      requestedFinalStageIds.current.clear();
      setPipeline(targetPipeline);
      setDeals([mapped]);
      setNextCursorByStage({});
      setLoadedStageIds({});
      setStageLoadErrorByStage({});
      setLoadingStageId(null);
      setLoading(true);
    } else {
      setDeals((current) => current.some((deal) => deal.id === mapped.id)
        ? current.map((deal) => deal.id === mapped.id ? { ...mapped, messages: deal.messages, tasks: deal.tasks } : deal)
        : [...current, mapped]);
    }
    if (targetStage.stageType !== "open") requestedFinalStageIds.current.add(targetStage.id);
    selectedDealIdRef.current = dealId;
    setSelectedDealId(dealId);
  }, [deals, isEmployee, pipelines, removeDealLocally, sources, users]);

  const loadStageDeals = useCallback(async (stageId: string) => {
    if (loadedStageIds[stageId] || loadingStageId) return;
    if (!remoteEnabled) {
      setLoadedStageIds((current) => ({ ...current, [stageId]: true }));
      return;
    }
    const requestedPipelineId = pipeline.id;
    const generation = dealRequestGeneration.current;
    requestedFinalStageIds.current.add(stageId);
    setLoadingStageId(stageId);
    setStageLoadErrorByStage((current) => ({ ...current, [stageId]: null }));
    try {
      const page = await api.get<CursorPage<ApiDeal>>(
        `/deals?limit=100&pipeline_id=${requestedPipelineId}&stage_id=${stageId}${dealSearch ? `&search=${encodeURIComponent(dealSearch)}` : ""}`,
      );
      if (generation !== dealRequestGeneration.current || activePipelineId.current !== requestedPipelineId) return;
      const mapped = page.items
        .filter((deal) => deal.pipeline_id === requestedPipelineId && deal.stage_id === stageId)
        .map((deal) => mapRemoteDeal(
          deal,
          pipeline.stages,
          users,
          sources,
          tasksByDealRef.current.get(deal.id) ?? [],
        ));
      setDeals((current) => {
        const withoutStage = current.filter((deal) => deal.stageId !== stageId);
        return [...withoutStage, ...mapped];
      });
      setNextCursorByStage((current) => ({ ...current, [stageId]: page.next_cursor }));
      setLoadedStageIds((current) => ({ ...current, [stageId]: true }));
    } catch (reason) {
      if (generation !== dealRequestGeneration.current) return;
      requestedFinalStageIds.current.delete(stageId);
      console.error("Pulse CRM deferred deal stage load failed", reason);
      setStageLoadErrorByStage((current) => ({
        ...current,
        [stageId]: "Не удалось загрузить сделки. Повторите попытку.",
      }));
    } finally {
      if (generation === dealRequestGeneration.current) {
        setLoadingStageId((current) => current === stageId ? null : current);
      }
    }
  }, [dealSearch, loadedStageIds, loadingStageId, pipeline, sources, users]);

  const loadMoreDeals = useCallback(async (stageId: string) => {
    const cursor = nextCursorByStage[stageId];
    if (!remoteEnabled || !cursor || loadingStageId) return;
    const generation = dealRequestGeneration.current;
    const requestedPipelineId = pipeline.id;
    setLoadingStageId(stageId);
    try {
      const page = await api.get<CursorPage<ApiDeal>>(
        `/deals?limit=100&pipeline_id=${requestedPipelineId}&stage_id=${stageId}&cursor=${encodeURIComponent(cursor)}${dealSearch ? `&search=${encodeURIComponent(dealSearch)}` : ""}`,
      );
      if (generation !== dealRequestGeneration.current || activePipelineId.current !== requestedPipelineId) return;
      const mapped = page.items
        .filter((deal) => deal.pipeline_id === requestedPipelineId && deal.stage_id === stageId)
        .map((deal) => mapRemoteDeal(
          deal,
          pipeline.stages,
          users,
          sources,
          tasksByDealRef.current.get(deal.id) ?? [],
        ));
      setDeals((current) => {
        const existing = new Set(current.map((deal) => deal.id));
        return [...current, ...mapped.filter((deal) => !existing.has(deal.id))];
      });
      setNextCursorByStage((current) => ({ ...current, [stageId]: page.next_cursor }));
    } finally {
      if (generation === dealRequestGeneration.current) setLoadingStageId(null);
    }
  }, [dealSearch, loadingStageId, nextCursorByStage, pipeline, sources, users]);

  const loadMoreDealSearch = useCallback(async () => {
    const cursor = nextDealSearchCursor;
    if (!remoteEnabled || !dealSearch || !cursor || loadingMoreDealSearch) return;
    const generation = dealRequestGeneration.current;
    const requestedPipelineId = pipeline.id;
    setLoadingMoreDealSearch(true);
    try {
      const page = await api.get<CursorPage<ApiDeal>>(
        `/deals?limit=100&pipeline_id=${requestedPipelineId}&search=${encodeURIComponent(dealSearch)}&cursor=${encodeURIComponent(cursor)}`,
      );
      if (generation !== dealRequestGeneration.current || activePipelineId.current !== requestedPipelineId) return;
      const mapped = page.items
        .filter((deal) => deal.pipeline_id === requestedPipelineId)
        .map((deal) => mapRemoteDeal(
          deal,
          pipeline.stages,
          users,
          sources,
          tasksByDealRef.current.get(deal.id) ?? [],
        ));
      setDeals((current) => {
        const nextById = new Map(current.map((deal) => [deal.id, deal]));
        for (const deal of mapped) {
          const existing = nextById.get(deal.id);
          nextById.set(deal.id, existing
            ? { ...deal, messages: existing.messages, tasks: existing.tasks }
            : deal);
        }
        return [...nextById.values()];
      });
      setNextDealSearchCursor(page.next_cursor);
    } catch (reason) {
      if (generation !== dealRequestGeneration.current) return;
      console.error("Pulse CRM deal search pagination failed", reason);
      throw reason;
    } finally {
      if (generation === dealRequestGeneration.current) setLoadingMoreDealSearch(false);
    }
  }, [dealSearch, loadingMoreDealSearch, nextDealSearchCursor, pipeline, sources, users]);

  const moveDeal = useCallback(async (dealId: string, stageId: string) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current || current.stageId === stageId) return;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId
          ? { ...deal, stageId, version: deal.version + 1 }
          : deal));
        return;
      }
      try {
        const persisted = await api.patch<ApiDeal>(`/deals/${dealId}/stage`, {
          target_stage_id: stageId,
          expected_version: current.version,
        });
        mergeRemoteDeal(persisted);
      } catch (reason) {
        await recoverVersionedDeal(dealId, reason);
        throw reason;
      }
    });
  }, [deals, mergeRemoteDeal, recoverVersionedDeal, runDealMutation]);

  const setNextPurchase = useCallback(async (dealId: string, date: string | null) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    const nextPurchaseAt = date
      ? new Date(`${date}T09:00:00`).toISOString()
      : undefined;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId
          ? { ...deal, nextPurchaseAt, version: deal.version + 1 }
          : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { next_purchase_at: nextPurchaseAt ?? null });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const setDealContact = useCallback(async (
    dealId: string,
    contact: { id: string; name: string; phone?: string; email?: string } | null,
  ) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId ? {
          ...deal,
          contactIds: contact ? [contact.id] : [],
          contactName: contact?.name,
          phone: contact?.phone,
          email: contact?.email,
          version: deal.version + 1,
        } : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { contact_ids: contact ? [contact.id] : [] });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const setDealCompany = useCallback(async (
    dealId: string,
    company: { id: string; name: string } | null,
  ) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId ? {
          ...deal,
          companyId: company?.id,
          companyName: company?.name,
          version: deal.version + 1,
        } : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { company_id: company?.id ?? null });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const setDealAssignee = useCallback(async (dealId: string, assignee: UserSummary | null) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId
          ? { ...deal, assignee: assignee ?? userSummary(undefined), version: deal.version + 1 }
          : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { assignee_id: assignee?.id ?? null });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const setDealTags = useCallback(async (dealId: string, tags: string[]) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    const normalizedTags = normalizeDealTags(tags);
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId
          ? { ...deal, tags: normalizedTags, version: deal.version + 1 }
          : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { tags: normalizedTags });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const setDealCustomFields = useCallback(async (
    dealId: string,
    fields: Record<string, unknown>,
  ) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    await runDealMutation(dealId, async () => {
      if (!remoteEnabled) {
        setDeals((items) => items.map((deal) => deal.id === dealId
          ? { ...deal, customFields: fields, version: deal.version + 1 }
          : deal));
        return;
      }
      await persistDealPatch(dealId, current.version, { custom_fields: fields });
    });
  }, [deals, persistDealPatch, runDealMutation]);

  const addDeal = useCallback(async (input: NewDealInput) => {
    const sourceLabels: Record<SourceCode, string> = {
      manual: "ручной ввод",
      email: "email",
      telegram: "Telegram",
      max: "MAX",
      webhook: "webhook",
      html_form: "форма на сайте",
      amo_import: "amoCRM",
    };

    const optimistic: Deal = {
      id: `deal-${crypto.randomUUID()}`,
      title: input.title,
      subtitle: input.subtitle,
      amount: input.amount,
      currency: "RUB",
      source: input.source,
      sourceLabel: sourceLabels[input.source],
      assignee: isEmployee ? currentUser : initialDeals[3].assignee,
      dueDate: new Date(Date.now() + 86_400_000).toISOString().slice(0, 10),
      stageId: pipeline.stages[0]?.id ?? demoPipeline.stages[0].id,
      status: "open",
      contactName: input.title,
      tags: [],
      customFields: { subtitle: input.subtitle },
      version: 1,
      messages: [],
      tasks: [],
    };

    setDeals((items) => [optimistic, ...items]);
    const creationGeneration = ++openDealGeneration.current;
    selectedDealIdRef.current = optimistic.id;
    setSelectedDealId(optimistic.id);

    if (!remoteEnabled) return optimistic;

    try {
      const firstStage = pipeline.stages[0];
      if (!firstStage) throw new Error("В активной воронке нет этапов");
      const sourceId = sources.find((source) => source.key === input.source)?.id ?? null;
      const created = await api.post<ApiDeal>("/deals", {
        title: input.title,
        pipeline_id: pipeline.id,
        stage_id: firstStage.id,
        amount: input.amount,
        currency: "RUB",
        source_id: sourceId,
        tags: [],
        custom_fields: { subtitle: input.subtitle },
        ...(isEmployee ? { assignee_id: currentUser.id } : {}),
      });
      const persisted = { ...optimistic, id: created.id, tags: created.tags, version: created.version };
      if (creationGeneration !== openDealGeneration.current) return persisted;
      setDeals((items) => items.map((deal) => (deal.id === optimistic.id ? persisted : deal)));
      selectedDealIdRef.current = persisted.id;
      setSelectedDealId(persisted.id);
      return persisted;
    } catch (error) {
      if (creationGeneration !== openDealGeneration.current) throw error;
      setDeals((items) => items.filter((deal) => deal.id !== optimistic.id));
      selectedDealIdRef.current = null;
      setSelectedDealId(null);
      throw error;
    }
  }, [currentUser, isEmployee, pipeline, sources]);

  const deleteDeal = useCallback(async (dealId: string) => {
    const current = deals.find((deal) => deal.id === dealId);
    if (!current) return;
    await runDealMutation(dealId, async () => {
      if (remoteEnabled) {
        try {
          await api.delete(`/deals/${dealId}?expected_version=${current.version}`);
        } catch (reason) {
          if (reason instanceof ApiError && reason.status === 404) {
            removeDealLocally(dealId);
            return;
          }
          await recoverVersionedDeal(dealId, reason);
          throw reason;
        }
      }
      removeDealLocally(dealId);
    });
  }, [deals, recoverVersionedDeal, removeDealLocally, runDealMutation]);

  const sendMessage = useCallback(async (dealId: string, body: string, attachment?: File) => {
    const message: Message = {
      id: `message-${crypto.randomUUID()}`,
      body,
      direction: "outbound",
      createdAt: new Date().toISOString(),
      status: remoteEnabled ? "queued" : "sent",
    };

    setDeals((items) =>
      items.map((deal) =>
        deal.id === dealId ? { ...deal, messages: [...deal.messages, message] } : deal,
      ),
    );

    if (!remoteEnabled) return;
    try {
      let persisted: ApiMessage;
      if (attachment) {
        const form = new FormData();
        form.set("body", body);
        form.set("file", attachment);
        const response = await api.upload<ApiMessageWithAttachment>(`/deals/${dealId}/messages/with-attachment`, form);
        persisted = response.message;
      } else {
        persisted = await api.post<ApiMessage>(`/deals/${dealId}/messages`, { body });
      }
      setDeals((items) => items.map((deal) => deal.id === dealId
        ? {
          ...deal,
          messages: deal.messages.map((item) => item.id === message.id ? {
            id: persisted.id,
            body: persisted.body,
            direction: persisted.direction,
            createdAt: persisted.created_at,
            status: persisted.status,
            lastError: persisted.last_error ?? undefined,
          } : item),
        }
        : deal));
    } catch (reason) {
      setDeals((items) => items.map((deal) => deal.id === dealId
        ? { ...deal, messages: deal.messages.filter((item) => item.id !== message.id) }
        : deal));
      throw reason;
    }
  }, []);

  const retryMessage = useCallback(async (dealId: string, messageId: string) => {
    setDeals((items) => items.map((deal) => deal.id === dealId
      ? {
        ...deal,
        messages: deal.messages.map((message) => message.id === messageId
          ? { ...message, status: "queued", lastError: undefined }
          : message),
      }
      : deal));
    if (!remoteEnabled) return;
    try {
      const persisted = await api.post<ApiMessage>(`/messages/${messageId}/retry`, {});
      setDeals((items) => items.map((deal) => deal.id === dealId
        ? {
          ...deal,
          messages: deal.messages.map((message) => message.id === messageId
            ? {
              ...message,
              status: persisted.status,
              createdAt: persisted.created_at,
              lastError: persisted.last_error ?? undefined,
            }
            : message),
        }
        : deal));
    } catch (reason) {
      setDeals((items) => items.map((deal) => deal.id === dealId
        ? {
          ...deal,
          messages: deal.messages.map((message) => message.id === messageId
            ? { ...message, status: "failed", lastError: "Повторная отправка не запущена" }
            : message),
        }
        : deal));
      throw reason;
    }
  }, []);

  const toggleTask = useCallback(async (dealId: string, taskId: string) => {
    const currentDeal = deals.find((deal) => deal.id === dealId);
    const currentTask = currentDeal?.tasks.find((task) => task.id === taskId);
    if (!currentDeal || !currentTask) return;
    const completed = !currentTask.completed;
    setDeals((items) => items.map((deal) => deal.id === dealId
      ? {
        ...deal,
        tasks: deal.tasks.map((task) => task.id === taskId
          ? { ...task, completed, version: task.version + 1 }
          : task),
      }
      : deal));

    if (!remoteEnabled) return;
    try {
      const persisted = await api.patch<ApiTask>(`/tasks/${taskId}`, {
        expected_version: currentTask.version,
        status: completed ? "completed" : "open",
      });
      const snapshot = tasksByDealRef.current.get(dealId) ?? [];
      tasksByDealRef.current = new Map(tasksByDealRef.current).set(
        dealId,
        snapshot.map((task) => task.id === taskId ? persisted : task),
      );
      setDeals((items) => items.map((deal) => deal.id === dealId
        ? {
          ...deal,
          tasks: deal.tasks.map((task) => task.id === taskId
            ? { ...task, completed: persisted.status === "completed", version: persisted.version }
            : task),
        }
        : deal));
    } catch (reason) {
      setDeals((items) => items.map((deal) => deal.id === dealId ? currentDeal : deal));
      throw reason;
    }
  }, [deals]);

  const value = useMemo<CrmStore>(
    () => ({
      currentUser,
      isEmployee,
      deals,
      pipeline,
      pipelines,
      loading,
      error,
      selectedDealId,
      selectedDeal,
      selectedDealMutationPending,
      dealAssignees,
      selectDeal,
      openDeal,
      selectPipeline,
      moveDeal,
      setNextPurchase,
      setDealContact,
      setDealCompany,
      setDealAssignee,
      setDealTags,
      setDealSearch,
      setDealCustomFields,
      nextCursorByStage,
      nextDealSearchCursor,
      loadedStageIds,
      stageLoadErrorByStage,
      loadingStageId,
      loadingMoreDealSearch,
      loadStageDeals,
      loadMoreDeals,
      loadMoreDealSearch,
      addDeal,
      deleteDeal,
      sendMessage,
      retryMessage,
      toggleTask,
    }),
    [addDeal, currentUser, dealAssignees, deals, deleteDeal, error, isEmployee, loadMoreDealSearch, loadMoreDeals, loadStageDeals, loadedStageIds, loading, loadingMoreDealSearch, loadingStageId, moveDeal, nextCursorByStage, nextDealSearchCursor, openDeal, pipeline, pipelines, retryMessage, selectDeal, selectPipeline, selectedDeal, selectedDealId, selectedDealMutationPending, sendMessage, setDealAssignee, setDealCompany, setDealContact, setDealCustomFields, setDealSearch, setDealTags, setNextPurchase, stageLoadErrorByStage, toggleTask],
  );

  return <CrmContext.Provider value={value}>{children}</CrmContext.Provider>;
}

export function useCrm(): CrmStore {
  const store = useContext(CrmContext);
  if (!store) throw new Error("useCrm must be used inside CrmProvider");
  return store;
}

export function useDeferredSelection() {
  const { selectDeal } = useCrm();
  return useCallback((dealId: string | null) => {
    startTransition(() => selectDeal(dealId));
  }, [selectDeal]);
}
