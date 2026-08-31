import * as Dialog from "@radix-ui/react-dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, Check, ChevronLeft, ChevronRight, Circle, Plus, Search, Trash2, X } from "lucide-react";
import { useDeferredValue, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import { tasks as seedTasks, users as demoUsers } from "../data/demo";
import { api, remoteEnabled } from "../lib/api";
import { deepLinkEntityId } from "../lib/deep-links";
import { useCrm } from "../state/crm-store";
import type { ApiTask, ApiUser, CursorPage } from "../types/api";
import type { TaskItem } from "../types/crm";

type TaskFilter = "all" | "today" | "overdue" | "upcoming";

const TASKS_PAGE_SIZE = 25;
const EDITABLE_TASK_TYPES = ["follow_up", "meeting", "purchase"] as const;

const taskFilterLabels: Record<TaskFilter, string> = {
  all: "Все",
  today: "Сегодня",
  overdue: "Просрочено",
  upcoming: "Предстоящие",
};

function toDateTimeLocal(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 16);
}

function formatTaskDueAt(value: Date) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(value);
}

function preservedTaskTypeLabel(value: string) {
  if (value === "next_purchase") return "Следующая покупка (автоматически)";
  if (value.startsWith("amocrm:")) return "Импортированный тип amoCRM";
  return `Другой тип: ${value}`;
}

function taskPagePath(
  cursor: string | null,
  filter: TaskFilter,
  showCompleted: boolean,
  search: string,
) {
  const params = new URLSearchParams({ limit: String(TASKS_PAGE_SIZE) });
  if (filter !== "all") params.set("scope", filter);
  if (showCompleted) params.set("include_completed", "true");
  if (search) params.set("search", search);
  if (cursor) params.set("cursor", cursor);
  return `/tasks?${params.toString()}`;
}

export default function TasksPage() {
  const { currentUser, deals, isEmployee } = useCrm();
  const [tasks, setTasks] = useState<TaskItem[]>(() => seedTasks);
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [showCompleted, setShowCompleted] = useState(false);
  const [taskCursors, setTaskCursors] = useState<Array<string | null>>([null]);
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [editingTaskSnapshot, setEditingTaskSnapshot] = useState<TaskItem | null>(null);
  const [editingApiTask, setEditingApiTask] = useState<ApiTask | null>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [deletingTaskSnapshot, setDeletingTaskSnapshot] = useState<TaskItem | null>(null);
  const [deletingApiTask, setDeletingApiTask] = useState<ApiTask | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [createError, setCreateError] = useState("");
  const [editError, setEditError] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [accessRevision, setAccessRevision] = useState(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const routedTaskId = deepLinkEntityId(searchParams, "task");
  const queryClient = useQueryClient();
  const taskCursor = taskCursors.at(-1) ?? null;
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase("ru"));
  const taskQuery = useQuery({
    queryKey: ["tasks", filter, showCompleted, deferredSearch, taskCursor, accessRevision],
    queryFn: ({ signal }) => api.get<CursorPage<ApiTask>>(
      taskPagePath(taskCursor, filter, showCompleted, deferredSearch),
      { signal },
    ),
    enabled: remoteEnabled,
  });
  const userQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<ApiUser[]>("/users"),
    enabled: remoteEnabled,
  });
  const pagedRoutedApiTask = routedTaskId
    ? taskQuery.data?.items.find((task) => task.id === routedTaskId) ?? null
    : null;
  const taskDetailQuery = useQuery({
    queryKey: ["tasks", "detail", routedTaskId],
    queryFn: ({ signal }) => api.get<ApiTask>(`/tasks/${encodeURIComponent(routedTaskId!)}`, { signal }),
    enabled: remoteEnabled
      && Boolean(routedTaskId)
      && taskQuery.isSuccess
      && !pagedRoutedApiTask,
  });
  const routedApiTask = pagedRoutedApiTask ?? taskDetailQuery.data ?? null;
  const remoteTaskItems = useMemo<TaskItem[]>(() => {
    const usersById = new Map((userQuery.data ?? []).map((user) => [user.id, user]));
    const now = new Date();
    const tomorrow = new Date(now);
    tomorrow.setHours(24, 0, 0, 0);
    const apiTasks = taskQuery.data?.items ?? [];
    const visibleApiTasks = routedApiTask && !apiTasks.some((task) => task.id === routedApiTask.id)
      ? [...apiTasks, routedApiTask]
      : apiTasks;
    return visibleApiTasks.map((task) => {
      const user = usersById.get(task.assignee_id);
      const dueDate = new Date(task.due_at);
      const currentAssignee = task.assignee_id === currentUser.id ? currentUser : null;
      const name = currentAssignee?.name ?? user?.full_name ?? "Не назначен";
      return {
        id: task.id,
        title: task.title,
        entity: task.deal_id ? "Сделка" : task.contact_id ? "Контакт" : "Общая задача",
        dueAt: formatTaskDueAt(new Date(task.due_at)),
        status: task.status !== "open" ? "done" : dueDate < now ? "overdue" : dueDate < tomorrow ? "today" : "upcoming",
        assignee: {
          id: task.assignee_id,
          name,
          initials: name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru"),
          tone: currentAssignee?.tone ?? demoUsers.ak.tone,
        },
      };
    });
  }, [currentUser, routedApiTask, taskQuery.data, userQuery.data]);
  const sourceTasks = remoteEnabled ? remoteTaskItems : tasks;
  const editingTask = editingTaskSnapshot;
  const deletingTask = deletingTaskSnapshot;
  const editingTaskType = editingApiTask?.task_type ?? "follow_up";
  const preservesUnknownTaskType = !EDITABLE_TASK_TYPES.some((value) => value === editingTaskType);
  const editingDealId = editingApiTask?.deal_id ?? "";
  const preservesUnloadedDeal = Boolean(editingDealId && !deals.some((deal) => deal.id === editingDealId));
  const visible = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru");
    return sourceTasks.filter((task) => {
      const matchesCompletion = showCompleted || task.status !== "done";
      const matchesFilter = remoteEnabled || filter === "all" || task.status === filter;
      const matchesSearch = remoteEnabled || !needle || `${task.title} ${task.entity} ${task.assignee.name}`.toLocaleLowerCase("ru").includes(needle);
      return matchesCompletion && matchesFilter && matchesSearch;
    });
  }, [filter, search, showCompleted, sourceTasks]);

  useEffect(() => {
    if (!remoteEnabled) return;
    const handleAccessChanged = () => {
      const taskId = routedTaskId ?? editingTaskId;
      if (taskId) queryClient.removeQueries({ queryKey: ["tasks", "detail", taskId] });
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("task");
        return next;
      }, { replace: true });
      setEditingTaskId(null);
      setEditingTaskSnapshot(null);
      setEditingApiTask(null);
      setAccessRevision((value) => value + 1);
    };
    window.addEventListener("pulse:access-changed", handleAccessChanged);
    return () => window.removeEventListener("pulse:access-changed", handleAccessChanged);
  }, [editingTaskId, queryClient, routedTaskId, setSearchParams]);

  useEffect(() => {
    if (!routedTaskId) {
      if (editingTaskId) {
        setEditingTaskId(null);
        setEditingTaskSnapshot(null);
        setEditingApiTask(null);
      }
      return;
    }
    if (editingTaskId === routedTaskId) return;
    const taskItem = sourceTasks.find((task) => task.id === routedTaskId);
    if (!taskItem || (remoteEnabled && (!routedApiTask || userQuery.isLoading))) return;
    setEditError("");
    setEditingTaskId(routedTaskId);
    setEditingTaskSnapshot(taskItem);
    setEditingApiTask(remoteEnabled ? routedApiTask : null);
  }, [editingTaskId, routedApiTask, routedTaskId, sourceTasks, userQuery.isLoading]);

  const currentPage = taskCursors.length;
  const nextCursor = taskQuery.data?.next_cursor;
  const showPagination = remoteEnabled && (currentPage > 1 || Boolean(nextCursor));

  function selectFilter(value: TaskFilter) {
    setFilter(value);
    setTaskCursors([null]);
  }

  function toggleCompletedVisibility() {
    setShowCompleted((value) => !value);
    setTaskCursors([null]);
  }

  function showNextPage() {
    if (!nextCursor) return;
    setTaskCursors((cursors) => cursors.at(-1) === nextCursor ? cursors : [...cursors, nextCursor]);
  }

  function showPreviousPage() {
    setTaskCursors((cursors) => cursors.length > 1 ? cursors.slice(0, -1) : cursors);
  }

  function updateSearch(value: string) {
    setSearch(value);
    setTaskCursors([null]);
  }

  async function refreshTasks() {
    await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    window.dispatchEvent(new Event("pulse:tasks-refresh"));
  }

  function openTaskEditor(taskId: string) {
    const next = new URLSearchParams(searchParams);
    next.set("task", taskId);
    setSearchParams(next);
    setEditError("");
    setEditingTaskId(taskId);
    setEditingTaskSnapshot(sourceTasks.find((task) => task.id === taskId) ?? null);
    setEditingApiTask(remoteEnabled
      ? taskQuery.data?.items.find((task) => task.id === taskId) ?? null
      : null);
  }

  function openDeleteConfirmation(taskId: string) {
    if (isEmployee) return;
    setDeleteError("");
    setDeletingTaskId(taskId);
    setDeletingTaskSnapshot(sourceTasks.find((task) => task.id === taskId) ?? null);
    setDeletingApiTask(remoteEnabled
      ? taskQuery.data?.items.find((task) => task.id === taskId) ?? null
      : null);
  }

  function closeTaskEditor() {
    const next = new URLSearchParams(searchParams);
    next.delete("task");
    setSearchParams(next, { replace: true });
    setEditingTaskId(null);
    setEditingTaskSnapshot(null);
    setEditingApiTask(null);
  }

  function closeDeleteConfirmation() {
    setDeletingTaskId(null);
    setDeletingTaskSnapshot(null);
    setDeletingApiTask(null);
  }

  async function toggleTask(taskId: string) {
    if (remoteEnabled) {
      const task = taskQuery.data?.items.find((item) => item.id === taskId);
      if (!task) return;
      await api.patch(`/tasks/${taskId}`, {
        expected_version: task.version,
        status: task.status === "open" ? "completed" : "open",
      });
      await refreshTasks();
      return;
    }
    setTasks((items) => items.map((task) => task.id === taskId ? { ...task, status: task.status === "done" ? "today" : "done" } : task));
  }

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const title = String(data.get("title") ?? "").trim();
    const dueAt = new Date(String(data.get("due_at") ?? ""));
    const remindRaw = String(data.get("remind_at") ?? "");
    const assigneeId = isEmployee ? currentUser.id : String(data.get("assignee_id") ?? "");
    setCreating(true);
    setCreateError("");
    try {
      if (remoteEnabled) {
        await api.post<ApiTask>("/tasks", {
          title,
          description: String(data.get("description") ?? "").trim() || null,
          task_type: String(data.get("task_type") ?? "follow_up"),
          due_at: dueAt.toISOString(),
          remind_at: remindRaw ? new Date(remindRaw).toISOString() : null,
          assignee_id: assigneeId,
          deal_id: String(data.get("deal_id") ?? "") || null,
        });
        setTaskCursors([null]);
        await refreshTasks();
      } else {
        const assignee = isEmployee
          ? currentUser
          : Object.values(demoUsers).find((user) => user.id === assigneeId) ?? demoUsers.ak;
        setTasks((items) => [{
          id: `task-${crypto.randomUUID()}`,
          title,
          entity: "Общая задача",
          dueAt: formatTaskDueAt(dueAt),
          status: "upcoming",
          assignee,
        }, ...items]);
      }
      form.reset();
      setCreateOpen(false);
    } catch {
      setCreateError("Не удалось создать задачу. Проверьте срок и исполнителя.");
    } finally {
      setCreating(false);
    }
  }

  async function updateTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingTaskId || !editingTask) return;
    const data = new FormData(event.currentTarget);
    const dueAt = new Date(String(data.get("due_at") ?? ""));
    const remindRaw = String(data.get("remind_at") ?? "");
    const assigneeId = String(data.get("assignee_id") ?? "");
    const title = String(data.get("title") ?? "").trim();

    setEditing(true);
    setEditError("");
    try {
      if (remoteEnabled) {
        if (!editingApiTask) throw new Error("Task version is unavailable");
        await api.patch<ApiTask>(`/tasks/${editingTaskId}`, {
          expected_version: editingApiTask.version,
          title,
          description: String(data.get("description") ?? "").trim() || null,
          task_type: String(data.get("task_type") ?? "follow_up"),
          due_at: dueAt.toISOString(),
          remind_at: remindRaw ? new Date(remindRaw).toISOString() : null,
          ...(!isEmployee ? {
            assignee_id: assigneeId,
            deal_id: String(data.get("deal_id") ?? "") || null,
          } : {}),
        });
        await refreshTasks();
      } else {
        const assignee = Object.values(demoUsers).find((user) => user.id === assigneeId) ?? editingTask.assignee;
        setTasks((items) => items.map((task) => task.id === editingTaskId
          ? { ...task, title, dueAt: formatTaskDueAt(dueAt), assignee }
          : task));
      }
      closeTaskEditor();
    } catch {
      setEditError("Не удалось сохранить задачу. Обновите страницу и попробуйте снова.");
    } finally {
      setEditing(false);
    }
  }

  async function deleteTask() {
    if (isEmployee || !deletingTaskId || !deletingTask) return;
    setDeleting(true);
    setDeleteError("");
    try {
      if (remoteEnabled) {
        if (!deletingApiTask) throw new Error("Task version is unavailable");
        await api.delete(`/tasks/${deletingTaskId}?expected_version=${deletingApiTask.version}`);
        queryClient.setQueriesData<CursorPage<ApiTask>>({ queryKey: ["tasks"] }, (page) => page
          ? { ...page, items: page.items.filter((task) => task.id !== deletingTaskId) }
          : page);
        setTaskCursors([null]);
        await refreshTasks();
      } else {
        setTasks((items) => items.filter((task) => task.id !== deletingTaskId));
      }
      closeDeleteConfirmation();
    } catch {
      setDeleteError("Не удалось удалить задачу. Обновите страницу и попробуйте снова.");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="page tasks-page">
      <header className="page-header"><div><h1>Задачи</h1><p>{remoteEnabled ? `Страница ${currentPage} · по ${TASKS_PAGE_SIZE} задач` : "План работы команды и контроль сроков"}</p></div><Button variant="primary" className="tasks-page__desktop-add" onClick={() => setCreateOpen(true)} aria-controls="new-task-dialog"><Plus size={17} /> Новая задача</Button></header>
      <div className="content-toolbar tasks-toolbar">
        <div className="segment-control tasks-filter-segments" role="group" aria-label="Статус задач">
          {(["all", "today", "overdue", "upcoming"] as const).map((value) => {
            return <button key={value} type="button" className={filter === value ? "is-active" : ""} aria-pressed={filter === value} onClick={() => selectFilter(value)}>{taskFilterLabels[value]}</button>;
          })}
        </div>
        <label className="select-control tasks-filter-select">
          <span className="sr-only">Статус задач</span>
          <select aria-label="Статус задач на мобильном" value={filter} onChange={(event) => selectFilter(event.target.value as TaskFilter)}>
            {(Object.entries(taskFilterLabels) as Array<[TaskFilter, string]>).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <Button compact className={showCompleted ? "is-active" : ""} aria-pressed={showCompleted} onClick={toggleCompletedVisibility}>{showCompleted ? "Скрыть закрытые" : "Показать закрытые"}</Button>
        <label className="search-control"><Search size={18} /><input value={search} onChange={(event) => updateSearch(event.target.value)} placeholder="Поиск по задачам" /></label>
      </div>
      {taskQuery.isLoading || userQuery.isLoading ? <div className="route-loading" role="status">Загружаем задачи…</div> : null}
      {taskQuery.isError || userQuery.isError ? <div className="load-error" role="alert">Не удалось загрузить задачи</div> : null}
      {routedTaskId && taskDetailQuery.isError ? <div className="load-error" role="alert">Задача не найдена или недоступна</div> : null}
      {!taskQuery.isLoading && !userQuery.isLoading && !taskQuery.isError && !userQuery.isError ? <section className="task-board">
        <header><span>Статус</span><span>Задача</span><span>Срок</span><span>Исполнитель</span><span>Действия</span></header>
        {visible.map((task) => {
          const taskDealId = remoteEnabled
            ? (taskQuery.data?.items.find((item) => item.id === task.id) ?? (routedApiTask?.id === task.id ? routedApiTask : null))?.deal_id ?? null
            : null;
          return (
            <div
              className={`task-table-row task-table-row--${task.status}`}
              key={task.id}
            >
              <button type="button" className="task-table-row__open" aria-label={`Открыть задачу «${task.title}»`} onClick={() => openTaskEditor(task.id)} />
              <button
                type="button"
                className="task-check"
                onClick={() => void toggleTask(task.id)}
                aria-label={`${task.status === "done" ? "Возобновить" : "Завершить"} задачу «${task.title}»`}
              >
                {task.status === "done" ? <Check size={16} /> : <Circle size={16} />}
              </button>
              <span className="task-table-row__summary">
                <strong>{task.title}</strong>
                <small className="task-table-row__meta">
                  <span>{task.entity}</span>
                  {taskDealId ? <Link
                    to={`/deals?deal=${encodeURIComponent(taskDealId)}`}
                    aria-label={`Открыть сделку для задачи «${task.title}»`}
                  >Открыть сделку <ArrowUpRight size={13} aria-hidden="true" /></Link> : null}
                </small>
              </span>
              <time>{task.dueAt}</time>
              <span className="owner-line"><Avatar user={task.assignee} size="sm" /> {task.assignee.name}</span>
              <span className="task-table-row__actions">
                {!isEmployee ? <button type="button" className="icon-button task-delete-button" onClick={() => openDeleteConfirmation(task.id)} aria-label={`Удалить задачу «${task.title}»`}><Trash2 size={16} aria-hidden="true" /></button> : null}
              </span>
            </div>
          );
        })}
        {!visible.length ? <p className="empty-copy">Задачи не найдены</p> : null}
      </section> : null}
      {!taskQuery.isLoading && showPagination ? <nav className="list-pagination" aria-label="Пагинация задач">
        <Button compact className="list-pagination__button" onClick={showPreviousPage} disabled={currentPage === 1 || taskQuery.isFetching} aria-label="Предыдущая страница"><ChevronLeft size={16} /> Назад</Button>
        <span className="list-pagination__status" aria-live="polite">Страница {currentPage}</span>
        <Button compact className="list-pagination__button" onClick={showNextPage} disabled={!nextCursor || taskQuery.isFetching} aria-label="Следующая страница">Далее <ChevronRight size={16} /></Button>
      </nav> : null}
      <button type="button" className="mobile-fab tasks-page__mobile-add" onClick={() => setCreateOpen(true)} aria-label="Добавить задачу" aria-controls="new-task-dialog"><Plus size={26} aria-hidden="true" /></button>
      <Dialog.Root open={createOpen} onOpenChange={setCreateOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content id="new-task-dialog" className="dialog-content">
            <div className="dialog-header"><div><Dialog.Title>Новая задача</Dialog.Title><Dialog.Description>Задайте срок, напоминание и исполнителя.</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close></div>
            <form className="form-stack" onSubmit={(event) => void createTask(event)}>
              <label className="field"><span>Название</span><input name="title" required autoFocus /></label>
              <label className="field"><span>Описание</span><textarea name="description" rows={3} /></label>
              <label className="field"><span>Тип</span><select name="task_type" defaultValue="follow_up"><option value="follow_up">Связаться</option><option value="meeting">Встреча</option><option value="purchase">Следующая покупка</option></select></label>
              <label className="field"><span>Срок</span><input name="due_at" type="datetime-local" required /></label>
              <label className="field"><span>Напомнить</span><input name="remind_at" type="datetime-local" /></label>
              {isEmployee
                ? <div className="field" aria-label="Исполнитель"><span>Исполнитель</span><strong>{currentUser.name}</strong><input name="assignee_id" type="hidden" value={currentUser.id} /></div>
                : <label className="field"><span>Исполнитель</span><select name="assignee_id" required defaultValue={remoteEnabled ? userQuery.data?.[0]?.id : demoUsers.ak.id}>{remoteEnabled ? (userQuery.data ?? []).map((user) => <option key={user.id} value={user.id}>{user.full_name}</option>) : Object.values(demoUsers).map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}</select></label>}
              <label className="field"><span>Сделка</span><select name="deal_id" defaultValue=""><option value="">Без привязки</option>{deals.map((deal) => <option key={deal.id} value={deal.id}>{deal.title}</option>)}</select></label>
              {createError ? <p className="form-error" role="alert">{createError}</p> : null}
              <div className="dialog-actions"><Dialog.Close asChild><Button type="button">Отмена</Button></Dialog.Close><Button type="submit" variant="primary" disabled={creating}>{creating ? "Создаём…" : "Создать задачу"}</Button></div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <Dialog.Root open={Boolean(editingTaskId)} onOpenChange={(open) => { if (!open && !editing) closeTaskEditor(); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="dialog-content" aria-describedby="edit-task-description">
            <div className="dialog-header"><div><Dialog.Title>Редактировать задачу</Dialog.Title><Dialog.Description id="edit-task-description">Измените детали, срок или исполнителя.</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close></div>
            {editingTask ? <form key={editingTask.id} className="form-stack" onSubmit={(event) => void updateTask(event)}>
              <label className="field"><span>Название</span><input name="title" required autoFocus defaultValue={editingApiTask?.title ?? editingTask.title} /></label>
              <label className="field"><span>Описание</span><textarea name="description" rows={3} defaultValue={editingApiTask?.description ?? ""} /></label>
              <label className="field"><span>Тип</span><select name="task_type" defaultValue={editingTaskType}>{preservesUnknownTaskType ? <option value={editingTaskType}>{preservedTaskTypeLabel(editingTaskType)}</option> : null}<option value="follow_up">Связаться</option><option value="meeting">Встреча</option><option value="purchase">Следующая покупка</option></select></label>
              <label className="field"><span>Срок</span><input name="due_at" type="datetime-local" required defaultValue={toDateTimeLocal(editingApiTask?.due_at ?? null)} /></label>
              <label className="field"><span>Напомнить</span><input name="remind_at" type="datetime-local" defaultValue={toDateTimeLocal(editingApiTask?.remind_at ?? null)} /></label>
              {isEmployee
                ? <div className="field" aria-label="Исполнитель"><span>Исполнитель</span><strong>{editingTask.assignee.name}</strong><input name="assignee_id" type="hidden" value={editingApiTask?.assignee_id ?? editingTask.assignee.id} /></div>
                : <label className="field"><span>Исполнитель</span><select name="assignee_id" required defaultValue={editingApiTask?.assignee_id ?? editingTask.assignee.id}>{remoteEnabled ? (userQuery.data ?? []).map((user) => <option key={user.id} value={user.id}>{user.full_name}</option>) : Object.values(demoUsers).map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}</select></label>}
              {!isEmployee ? <label className="field"><span>Сделка</span><select name="deal_id" defaultValue={editingDealId}><option value="">Без привязки</option>{preservesUnloadedDeal ? <option value={editingDealId}>Текущая сделка (вне загруженного списка)</option> : null}{deals.map((deal) => <option key={deal.id} value={deal.id}>{deal.title}</option>)}</select></label> : null}
              {editError ? <p className="form-error" role="alert">{editError}</p> : null}
              <div className="dialog-actions"><Dialog.Close asChild><Button type="button" disabled={editing}>Отмена</Button></Dialog.Close><Button type="submit" variant="primary" disabled={editing}>{editing ? "Сохраняем…" : "Сохранить"}</Button></div>
            </form> : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {!isEmployee ? <Dialog.Root open={Boolean(deletingTaskId)} onOpenChange={(open) => { if (!open && !deleting) closeDeleteConfirmation(); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="dialog-content task-delete-dialog" aria-describedby="delete-task-description">
            <div className="dialog-header"><div><Dialog.Title>Удалить задачу?</Dialog.Title><Dialog.Description id="delete-task-description">Это действие нельзя отменить.</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close></div>
            {deletingTask ? <p className="task-delete-dialog__copy">Задача <strong>«{deletingTask.title}»</strong> будет удалена без возможности восстановления.</p> : null}
            {deleteError ? <p className="form-error" role="alert">{deleteError}</p> : null}
            <div className="dialog-actions"><Dialog.Close asChild><Button type="button" disabled={deleting}>Отмена</Button></Dialog.Close><Button type="button" variant="danger" disabled={deleting} onClick={() => void deleteTask()}>{deleting ? "Удаляем…" : "Удалить задачу"}</Button></div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root> : null}
    </div>
  );
}
