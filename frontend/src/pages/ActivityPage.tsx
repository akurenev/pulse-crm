import { useQuery } from "@tanstack/react-query";
import { Activity, Filter, Search } from "lucide-react";
import { useDeferredValue, useMemo, useState } from "react";

import { Avatar } from "../components/Avatar";
import { activities, users } from "../data/demo";
import { api, remoteEnabled } from "../lib/api";
import type { ApiActivity, ApiUser, CursorPage } from "../types/api";
import type { ActivityItem } from "../types/crm";

export default function ActivityPage() {
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase("ru"));
  const activityQuery = useQuery({
    queryKey: ["activity"],
    queryFn: () => api.get<CursorPage<ApiActivity>>("/activity?limit=100"),
    enabled: remoteEnabled,
  });
  const userQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<ApiUser[]>("/users"),
    enabled: remoteEnabled,
  });
  const sourceActivities = useMemo<ActivityItem[]>(() => {
    if (!remoteEnabled) return activities.concat(activities.map((item) => ({ ...item, id: `${item.id}-copy`, createdAt: `Вчера, ${item.createdAt}` })));
    const usersById = new Map((userQuery.data ?? []).map((user) => [user.id, user]));
    return (activityQuery.data?.items ?? []).map((event) => {
      const actor = event.actor_id ? usersById.get(event.actor_id) : undefined;
      const name = actor?.full_name ?? "Система";
      const kind = (["deal", "message", "task", "contact"] as const).find((value) => event.entity_type.includes(value)) ?? "system";
      return {
        id: event.id,
        kind,
        title: event.event_type.replaceAll(".", " · "),
        detail: String(event.payload.title ?? event.payload.name ?? event.entity_type),
        createdAt: new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(event.occurred_at)),
        actor: {
          id: event.actor_id ?? "system",
          name,
          initials: name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru"),
          tone: users.ak.tone,
        },
      };
    });
  }, [activityQuery.data, userQuery.data]);
  const visibleActivities = useMemo(() => sourceActivities.filter((item) => {
    const matchesKind = kindFilter === "all" || item.kind === kindFilter;
    const matchesSearch = !deferredSearch
      || `${item.title} ${item.detail} ${item.actor.name}`.toLocaleLowerCase("ru").includes(deferredSearch);
    return matchesKind && matchesSearch;
  }), [deferredSearch, kindFilter, sourceActivities]);

  return (
    <div className="page activity-page">
      <header className="page-header"><div><h1>Активность</h1><p>Неизменяемая история действий команды и интеграций</p></div></header>
      <div className="content-toolbar">
        <label className="search-control"><Search size={18} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по событиям" /></label>
        <button type="button" className={`button button--secondary${filtersOpen ? " is-active" : ""}`} aria-expanded={filtersOpen} onClick={() => setFiltersOpen((current) => !current)}><Filter size={17} /> Фильтры</button>
        {filtersOpen ? <label className="select-control activity-kind-filter"><span className="sr-only">Тип события</span><select aria-label="Тип события" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}><option value="all">Все события</option><option value="deal">Сделки</option><option value="message">Сообщения</option><option value="task">Задачи</option><option value="contact">Контакты</option><option value="system">Система</option></select></label> : null}
      </div>
      {activityQuery.isLoading || userQuery.isLoading ? <div className="route-loading" role="status">Загружаем события…</div> : null}
      {activityQuery.isError || userQuery.isError ? <div className="load-error" role="alert">Не удалось загрузить активность</div> : null}
      <section className="activity-feed">
        <div className="activity-date">Сегодня</div>
        {visibleActivities.map((item) => (
          <article key={item.id} className="activity-event">
            <span className={`activity-event__icon activity-event__icon--${item.kind}`}><Activity size={17} /></span>
            <div><strong>{item.title}</strong><p>{item.detail}</p><span className="activity-actor"><Avatar user={item.actor} size="sm" /> {item.actor.name}</span></div>
            <time>{item.createdAt}</time>
          </article>
        ))}
        {!visibleActivities.length ? <p className="empty-copy">События не найдены</p> : null}
      </section>
    </div>
  );
}
