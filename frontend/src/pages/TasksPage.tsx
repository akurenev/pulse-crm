import * as Dialog from "@radix-ui/react-dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Circle, Plus, Search, X } from "lucide-react";
import { useDeferredValue, useMemo, useState, type FormEvent } from "react";

import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import { tasks as seedTasks, users as demoUsers } from "../data/demo";
import { api, remoteEnabled } from "../lib/api";
import { useCrm } from "../state/crm-store";
import type { ApiTask, ApiUser, CursorPage } from "../types/api";
import type { TaskItem } from "../types/crm";

type TaskFilter = "all" | "today" | "overdue" | "upcoming";

const TASKS_PAGE_SIZE = 25;

const taskFilterLabels: Record<TaskFilter, string> = {
  all: "Все",
  today: "Сегодня",
  overdue: "Просрочено",
  upcoming: "Предстоящие",
};

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
  const { deals } = useCrm();
  const [tasks, setTasks] = useState<TaskItem[]>(() => seedTasks);
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [showCompleted, setShowCompleted] = useState(false);
  const [taskCursors, setTaskCursors] = useState<Array<string | null>>([null]);
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const queryClient = useQueryClient();
  const taskCursor = taskCursors.at(-1) ?? null;
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase("ru"));
  const taskQuery = useQuery({
    queryKey: ["tasks", filter, showCompleted, deferredSearch, taskCursor],
    queryFn: () => api.get<CursorPage<ApiTask>>(taskPagePath(taskCursor, filter, showCompleted, deferredSearch)),
    enabled: remoteEnabled,
  });
  const userQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<ApiUser[]>("/users"),
    enabled: remoteEnabled,
  });
  const remoteTaskItems = useMemo<TaskItem[]>(() => {
    const usersById = new Map((userQuery.data ?? []).map((user) => [user.id, user]));
    const now = new Date();
    const tomorrow = new Date(now);
    tomorrow.setHours(24, 0, 0, 0);
    return (taskQuery.data?.items ?? []).map((task) => {
      const user = usersById.get(task.assignee_id);
      const dueDate = new Date(task.due_at);
      const name = user?.full_name ?? "Не назначен";
      return {
        id: task.id,
        title: task.title,
        entity: task.deal_id ? "Сделка" : task.contact_id ? "Контакт" : "Общая задача",
        dueAt: new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(task.due_at)),
        status: task.status !== "open" ? "done" : dueDate < now ? "overdue" : dueDate < tomorrow ? "today" : "upcoming",
        assignee: {
          id: task.assignee_id,
          name,
          initials: name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru"),
          tone: demoUsers.ak.tone,
        },
      };
    });
  }, [taskQuery.data, userQuery.data]);
  const sourceTasks = remoteEnabled ? remoteTaskItems : tasks;
  const visible = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru");
    return sourceTasks.filter((task) => {
      const matchesCompletion = showCompleted || task.status !== "done";
      const matchesFilter = remoteEnabled || filter === "all" || task.status === filter;
      const matchesSearch = remoteEnabled || !needle || `${task.title} ${task.entity} ${task.assignee.name}`.toLocaleLowerCase("ru").includes(needle);
      return matchesCompletion && matchesFilter && matchesSearch;
    });
  }, [filter, search, showCompleted, sourceTasks]);

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

  async function toggleTask(taskId: string) {
    if (remoteEnabled) {
      const task = taskQuery.data?.items.find((item) => item.id === taskId);
      if (!task) return;
      await api.patch(`/tasks/${taskId}`, {
        expected_version: task.version,
        status: task.status === "open" ? "completed" : "open",
      });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      window.dispatchEvent(new Event("pulse:tasks-refresh"));
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
    const assigneeId = String(data.get("assignee_id") ?? "");
    setSaving(true);
    setSaveError("");
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
        await queryClient.invalidateQueries({ queryKey: ["tasks"] });
        window.dispatchEvent(new Event("pulse:tasks-refresh"));
      } else {
        const assignee = Object.values(demoUsers).find((user) => user.id === assigneeId) ?? demoUsers.ak;
        setTasks((items) => [{
          id: `task-${crypto.randomUUID()}`,
          title,
          entity: "Общая задача",
          dueAt: new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(dueAt),
          status: "upcoming",
          assignee,
        }, ...items]);
      }
      form.reset();
      setCreateOpen(false);
    } catch {
      setSaveError("Не удалось создать задачу. Проверьте срок и исполнителя.");
    } finally {
      setSaving(false);
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
      {!taskQuery.isLoading && !userQuery.isLoading && !taskQuery.isError && !userQuery.isError ? <section className="task-board">
        <header><span>Статус</span><span>Задача</span><span>Срок</span><span>Исполнитель</span></header>
        {visible.map((task) => (
          <button type="button" className={`task-table-row task-table-row--${task.status}`} key={task.id} onClick={() => void toggleTask(task.id)}>
            <span className="task-check">{task.status === "done" ? <Check size={16} /> : <Circle size={16} />}</span>
            <span><strong>{task.title}</strong><small>{task.entity}</small></span>
            <time>{task.dueAt}</time>
            <span className="owner-line"><Avatar user={task.assignee} size="sm" /> {task.assignee.name}</span>
          </button>
        ))}
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
              <label className="field"><span>Исполнитель</span><select name="assignee_id" required defaultValue={remoteEnabled ? userQuery.data?.[0]?.id : demoUsers.ak.id}>{remoteEnabled ? (userQuery.data ?? []).map((user) => <option key={user.id} value={user.id}>{user.full_name}</option>) : Object.values(demoUsers).map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}</select></label>
              <label className="field"><span>Сделка</span><select name="deal_id" defaultValue=""><option value="">Без привязки</option>{deals.map((deal) => <option key={deal.id} value={deal.id}>{deal.title}</option>)}</select></label>
              {saveError ? <p className="form-error" role="alert">{saveError}</p> : null}
              <div className="dialog-actions"><Dialog.Close asChild><Button type="button">Отмена</Button></Dialog.Close><Button type="submit" variant="primary" disabled={saving}>{saving ? "Создаём…" : "Создать задачу"}</Button></div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
