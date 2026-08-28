import { useQuery } from "@tanstack/react-query";
import { ArrowRight, BellRing, CalendarClock, CircleAlert, Plus, TrendingUp } from "lucide-react";
import { useMemo } from "react";
import { Link } from "react-router-dom";

import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import { activities, tasks, users } from "../data/demo";
import { api, remoteEnabled } from "../lib/api";
import { formatMoney } from "../lib/format";
import { useAuth } from "../state/auth-store";
import { useCrm } from "../state/crm-store";
import type { ApiActivity, ApiDashboard, ApiTask, CursorPage } from "../types/api";
import type { ActivityItem, TaskItem } from "../types/crm";

export default function DashboardPage() {
  const { session } = useAuth();
  const { deals, pipeline } = useCrm();
  const taskQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.get<CursorPage<ApiTask>>("/tasks?limit=100"),
    enabled: remoteEnabled,
  });
  const activityQuery = useQuery({
    queryKey: ["activity"],
    queryFn: () => api.get<CursorPage<ApiActivity>>("/activity?limit=20"),
    enabled: remoteEnabled,
  });
  const metricsQuery = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<ApiDashboard>("/dashboard"),
    enabled: remoteEnabled,
  });
  const dashboardTasks = useMemo<TaskItem[]>(() => {
    if (!remoteEnabled) return tasks;
    const today = new Date().toISOString().slice(0, 10);
    return (taskQuery.data?.items ?? []).map((task) => ({
      id: task.id,
      title: task.title,
      entity: task.deal_id ? "Сделка" : task.contact_id ? "Контакт" : "Общая задача",
      dueAt: new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(task.due_at)),
      status: task.status === "completed" ? "done" : task.due_at.slice(0, 10) < today ? "overdue" : task.due_at.slice(0, 10) === today ? "today" : "upcoming",
      assignee: users.ak,
    }));
  }, [taskQuery.data]);
  const dashboardActivities = useMemo<ActivityItem[]>(() => {
    if (!remoteEnabled) return activities;
    return (activityQuery.data?.items ?? []).map((event) => ({
      id: event.id,
      kind: (["deal", "message", "task", "contact"] as const).find((value) => event.entity_type.includes(value)) ?? "system",
      title: event.event_type.replaceAll(".", " · "),
      detail: String(event.payload.title ?? event.payload.name ?? event.entity_type),
      createdAt: new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(event.occurred_at)),
      actor: users.ak,
    }));
  }, [activityQuery.data]);
  const pipelineTotal = deals.reduce((sum, deal) => sum + deal.amount, 0);
  const overdue = metricsQuery.data?.overdue_tasks ?? dashboardTasks.filter((task) => task.status === "overdue").length;
  const nextPurchases = metricsQuery.data?.upcoming_purchases_30d ?? deals.filter((deal) => deal.nextPurchaseAt).length;
  const newLeads = metricsQuery.data?.new_leads_24h ?? deals.filter((deal) => deal.stageId === pipeline.stages[0]?.id).length;
  const inactiveDeals = metricsQuery.data?.inactive_deals ?? deals.filter((deal) => {
    const due = new Date(deal.dueDate).getTime();
    return Number.isFinite(due) && due < Date.now() - 7 * 86_400_000;
  }).length;

  return (
    <div className="page dashboard-page">
      <header className="page-header dashboard-heading">
        <div><h1>Добрый день, {session?.user.full_name.split(/\s+/)[0] ?? "Алексей"}</h1><p>Вот что требует внимания сегодня.</p></div>
        <Button variant="primary"><Plus size={17} /> Новая сделка</Button>
      </header>

      <section className="attention-strip" aria-label="Важное уведомление">
        <CircleAlert size={20} />
        <div>
          <strong>{overdue ? `${overdue} просроченная задача` : "Просроченных задач нет"}</strong>
          <span>{overdue ? dashboardTasks.find((task) => task.status === "overdue")?.title : "Команда работает по плану"}</span>
        </div>
        <Link to="/tasks">Открыть задачи <ArrowRight size={16} /></Link>
      </section>

      <section className="metric-rail" aria-label="Сводка">
        <div><span><TrendingUp size={18} /> Активные сделки</span><strong>{deals.length}</strong><small>{formatMoney(pipelineTotal)} в работе</small></div>
        <div><span><BellRing size={18} /> Новые обращения</span><strong>{newLeads}</strong><small>{inactiveDeals} без активности более 7 дней</small></div>
        <div><span><CalendarClock size={18} /> Следующие покупки</span><strong>{nextPurchases}</strong><small>на ближайшие 30 дней</small></div>
      </section>

      <div className="dashboard-columns">
        <section className="open-panel dashboard-tasks">
          <header><div><h2>Задачи на сегодня</h2><p>Сначала просроченные и срочные</p></div><Link to="/tasks">Все задачи</Link></header>
          <div className="task-list">
            {dashboardTasks.slice(0, 3).map((task) => (
              <div key={task.id} className={`dashboard-task dashboard-task--${task.status}`}>
                <span className="task-dot" />
                <div><strong>{task.title}</strong><span>{task.entity}</span></div>
                <time>{task.dueAt}</time>
                <Avatar user={task.assignee} size="sm" />
              </div>
            ))}
          </div>
        </section>

        <section className="open-panel dashboard-activity">
          <header><div><h2>Последняя активность</h2><p>События команды и каналов</p></div><Link to="/activity">Вся история</Link></header>
          <ol className="activity-list">
            {dashboardActivities.slice(0, 4).map((item) => (
              <li key={item.id}>
                <Avatar user={item.actor} size="sm" />
                <div><strong>{item.title}</strong><span>{item.detail}</span></div>
                <time>{item.createdAt}</time>
              </li>
            ))}
          </ol>
        </section>
      </div>

      <section className="open-panel pipeline-overview">
        <header><div><h2>Конверсия по воронкам</h2><p>Доля выигранных сделок и распределение активной воронки</p></div><Link to="/deals">Открыть воронку</Link></header>
        <div className="pipeline-bars">
          {remoteEnabled && metricsQuery.data?.pipelines.length ? metricsQuery.data.pipelines.map((item) => (
            <div key={item.pipeline_id}>
              <span>{item.pipeline_name} · {item.won_deals}/{item.total_deals}</span>
              <i style={{ width: `${Math.max(3, item.conversion_percent)}%` }} />
              <strong>{item.conversion_percent}%</strong>
            </div>
          )) : pipeline.stages.map((stage) => {
            const stageDeals = deals.filter((deal) => deal.stageId === stage.id);
            return <div key={stage.id}><span>{stage.name}</span><i style={{ width: `${Math.max(18, stageDeals.length * 28)}%` }} /><strong>{stageDeals.length}</strong></div>;
          })}
        </div>
      </section>
    </div>
  );
}
