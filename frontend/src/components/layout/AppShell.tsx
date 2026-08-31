import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  Building2,
  CheckSquare2,
  ChevronLeft,
  ChevronRight,
  Home,
  LogOut,
  Menu,
  Settings,
  Users,
} from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";

import { users } from "../../data/demo";
import { api, remoteEnabled } from "../../lib/api";
import { useAuth } from "../../state/auth-store";
import { CrmProvider } from "../../state/crm-store";
import type { ApiInAppNotification } from "../../types/api";
import type { UserSummary } from "../../types/crm";
import { Avatar } from "../Avatar";
import { BrandMark } from "../BrandMark";
import { PushNotificationSettings } from "../PushNotificationSettings";

const navigation = [
  { to: "/", label: "Главная", icon: Home, end: true },
  { to: "/deals", label: "Сделки", icon: Building2 },
  { to: "/contacts", label: "Клиенты", icon: Users },
  { to: "/tasks", label: "Задачи", icon: CheckSquare2 },
  { to: "/activity", label: "Активность", icon: Activity, desktopOnly: true },
  { to: "/settings", label: "Настройки", icon: Settings, desktopOnly: true },
];

const realtimeEventTypes = [
  "deal.created",
  "deal.updated",
  "deal.stage_changed",
  "deal.assigned",
  "deal.deleted",
  "lead.created",
  "message.inbound.received",
  "message.outbound.queued",
  "message.outbound.sent",
  "task.created",
  "task.updated",
  "task.deleted",
  "task.due_soon",
  "task.overdue",
  "purchase.due_soon",
  "notification.delivered",
  "contact.created",
  "contact.updated",
  "contact.deleted",
  "company.created",
  "company.updated",
] as const;

const REALTIME_REFRESH_DEBOUNCE_MS = 250;
const REALTIME_FALLBACK_INTERVAL_MS = 15_000;

export function AppShell() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const { session, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const canManageSettings = session?.user.role === "owner" || session?.user.role === "admin";
  const name = session?.user.full_name ?? users.ak.name;
  const currentUser: UserSummary = {
    id: session?.user.id ?? users.ak.id,
    name,
    initials: name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru"),
    tone: "violet",
  };
  const mobileTitle = navigation.find((item) => item.to === location.pathname)?.label
    ?? (location.pathname === "/settings" ? "Настройки" : "Pulse CRM");
  useRealtimeRefresh();

  return (
    <CrmProvider>
      <div className={`app-shell${sidebarCollapsed ? " app-shell--collapsed" : ""}`}>
      <aside className="sidebar">
        <BrandMark />
        <nav className="sidebar__nav" aria-label="Основная навигация">
          {navigation.filter((item) => item.to !== "/settings" || canManageSettings).map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`}>
              <Icon size={20} strokeWidth={1.8} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <button className="sidebar__collapse" type="button" aria-expanded={!sidebarCollapsed} aria-label={sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"} onClick={() => setSidebarCollapsed((current) => !current)}>
          {sidebarCollapsed ? <ChevronRight size={18} aria-hidden="true" /> : <ChevronLeft size={18} aria-hidden="true" />}
          <span>{sidebarCollapsed ? "Развернуть" : "Свернуть"}</span>
        </button>
      </aside>

      <header className="mobile-header">
        <BrandMark compact />
        <span className="mobile-header__title">{mobileTitle}</span>
        <NotificationCenter size={21} />
        <UserMenu user={currentUser} email={session?.user.email ?? "owner@pulse.local"} onLogout={async () => { await logout(); navigate("/login", { replace: true }); }} />
      </header>

      <header className="topbar">
        <div />
        <NotificationCenter size={20} />
        <UserMenu user={currentUser} email={session?.user.email ?? "owner@pulse.local"} onLogout={async () => { await logout(); navigate("/login", { replace: true }); }} />
      </header>

      <main className="app-content">
        <Outlet />
      </main>

      <nav className="bottom-nav" aria-label="Мобильная навигация">
        {navigation.filter((item) => !item.desktopOnly).map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => `bottom-nav__item${isActive ? " bottom-nav__item--active" : ""}`}>
            <Icon size={22} strokeWidth={1.8} aria-hidden="true" />
            <span>{label}</span>
          </NavLink>
        ))}
        <NavLink to={canManageSettings ? "/settings" : "/activity"} className={({ isActive }) => `bottom-nav__item${isActive ? " bottom-nav__item--active" : ""}`}>
          <Menu size={22} strokeWidth={1.8} aria-hidden="true" />
          <span>Ещё</span>
        </NavLink>
      </nav>
      </div>
    </CrmProvider>
  );
}

const demoNotifications: ApiInAppNotification[] = [
  { id: "notice-1", subject: "Новый лид", body: "Кофейня «Слой» добавлена в воронку.", delivered_at: new Date().toISOString(), created_at: new Date().toISOString() },
  { id: "notice-2", subject: "Просрочена задача", body: "Связаться с Анной до 12:00.", delivered_at: new Date(Date.now() - 3_600_000).toISOString(), created_at: new Date(Date.now() - 3_600_000).toISOString() },
  { id: "notice-3", subject: "Следующая покупка", body: "Поставка для «Север Кофе» через 7 дней.", delivered_at: new Date(Date.now() - 7_200_000).toISOString(), created_at: new Date(Date.now() - 7_200_000).toISOString() },
];

function NotificationCenter({ size }: { size: number }) {
  const notificationsQuery = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get<ApiInAppNotification[]>("/notifications?limit=20"),
    enabled: remoteEnabled,
  });
  const notifications = remoteEnabled ? notificationsQuery.data ?? [] : demoNotifications;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="icon-button notification-button" aria-label="Уведомления">
        <Bell size={size} strokeWidth={1.8} />
        {notifications.length ? <span>{Math.min(99, notifications.length)}</span> : null}
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="notification-menu" align="end" sideOffset={8}>
          <header><strong>Уведомления</strong><small>{notifications.length ? `${notifications.length} последних` : "Новых событий нет"}</small></header>
          <PushNotificationSettings />
          <div>
            {notifications.map((notification) => (
              <DropdownMenu.Item key={notification.id} className="notification-menu__item">
                <span className="notification-menu__dot" />
                <span><strong>{notification.subject ?? "Pulse CRM"}</strong><small>{notification.body}</small><time>{new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(notification.delivered_at))}</time></span>
              </DropdownMenu.Item>
            ))}
            {!notifications.length ? <p>Здесь появятся напоминания и события по сделкам.</p> : null}
          </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function useRealtimeRefresh() {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!remoteEnabled) return;
    let refreshTimeout: number | null = null;
    let fallbackInterval: number | null = null;
    let disposed = false;

    const refresh = () => {
      refreshTimeout = null;
      if (disposed) return;
      void queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] !== "push",
      });
      window.dispatchEvent(new Event("pulse:refresh"));
    };

    const scheduleRefresh = () => {
      if (refreshTimeout !== null) return;
      refreshTimeout = window.setTimeout(refresh, REALTIME_REFRESH_DEBOUNCE_MS);
    };

    const stopFallback = () => {
      if (fallbackInterval === null) return;
      window.clearInterval(fallbackInterval);
      fallbackInterval = null;
    };

    const startFallback = () => {
      if (disposed || fallbackInterval !== null) return;
      fallbackInterval = window.setInterval(scheduleRefresh, REALTIME_FALLBACK_INTERVAL_MS);
    };

    if (typeof EventSource === "undefined") {
      startFallback();
      return () => {
        disposed = true;
        stopFallback();
        if (refreshTimeout !== null) window.clearTimeout(refreshTimeout);
      };
    }

    const source = new EventSource("/api/v1/events");
    const handleOpen = () => {
      stopFallback();
      // Reconcile the REST snapshot with events that may have arrived before
      // the server established the stream cursor, and after reconnect gaps.
      scheduleRefresh();
    };
    const handleError = () => startFallback();
    source.addEventListener("open", handleOpen);
    source.addEventListener("error", handleError);
    for (const eventType of realtimeEventTypes) source.addEventListener(eventType, scheduleRefresh);
    // Cover slow or failed initial connections. A successful `open` clears the
    // interval before it can poll while the live stream is healthy.
    startFallback();

    return () => {
      disposed = true;
      stopFallback();
      if (refreshTimeout !== null) window.clearTimeout(refreshTimeout);
      source.removeEventListener("open", handleOpen);
      source.removeEventListener("error", handleError);
      for (const eventType of realtimeEventTypes) source.removeEventListener(eventType, scheduleRefresh);
      source.close();
    };
  }, [queryClient]);
}

function UserMenu({ user, email, onLogout }: { user: UserSummary; email: string; onLogout: () => Promise<void> }) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="user-menu__trigger" aria-label={`Аккаунт ${user.name}`}>
        <Avatar user={user} size="lg" />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="user-menu" align="end" sideOffset={8}>
          <div className="user-menu__identity"><strong>{user.name}</strong><small>{email}</small></div>
          <DropdownMenu.Separator />
          <DropdownMenu.Item onSelect={() => void onLogout()}><LogOut size={16} /> Выйти</DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
