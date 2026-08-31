import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";

const { apiGetMock, authState, logoutMock, refreshSessionMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  authState: { role: "owner" as "owner" | "admin" | "manager" | "employee" },
  logoutMock: vi.fn(),
  refreshSessionMock: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  api: { get: apiGetMock },
  remoteEnabled: true,
}));

vi.mock("../../state/auth-store", () => ({
  useAuth: () => ({
    session: {
      user: {
        id: "user-test",
        email: "owner@example.com",
        full_name: "Тестовый Пользователь",
        role: authState.role,
      },
      workspace: { id: "workspace-test" },
    },
    logout: logoutMock,
    refreshSession: refreshSessionMock,
    expireSession: vi.fn(),
  }),
}));

vi.mock("../../state/crm-store", () => ({
  CrmProvider: ({ children }: PropsWithChildren) => children,
}));

type EventSourceListener = EventListenerOrEventListenerObject;

class EventSourceMock {
  static instances: EventSourceMock[] = [];

  readonly close = vi.fn();
  private readonly listeners = new Map<string, Set<EventSourceListener>>();

  constructor(readonly url: string) {
    EventSourceMock.instances.push(this);
  }

  addEventListener(type: string, listener: EventSourceListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventSourceListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventSourceListener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string) {
    const event = new Event(type);
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === "function") listener.call(this, event);
      else listener.handleEvent(event);
    }
  }

  listenerCount(type: string) {
    return this.listeners.get(type)?.size ?? 0;
  }
}

const originalEventSource = Object.getOwnPropertyDescriptor(globalThis, "EventSource");

function renderShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateQueries = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/deals"]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="deals" element={<div>Сделки</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, invalidateQueries, queryClient };
}

function currentSource() {
  const source = EventSourceMock.instances.at(-1);
  if (!source) throw new Error("EventSource was not created");
  return source;
}

beforeEach(() => {
  authState.role = "owner";
  vi.useFakeTimers();
  EventSourceMock.instances = [];
  apiGetMock.mockReset();
  apiGetMock.mockResolvedValue([]);
  logoutMock.mockReset();
  refreshSessionMock.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(globalThis, "EventSource", {
    configurable: true,
    writable: true,
    value: EventSourceMock,
  });
});

afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  if (originalEventSource) Object.defineProperty(globalThis, "EventSource", originalEventSource);
  else Reflect.deleteProperty(globalThis, "EventSource");
});

describe("AppShell realtime refresh", () => {
  it("hides settings navigation from employees", () => {
    authState.role = "employee";
    renderShell();

    expect(document.querySelector('a[href="/settings"]')).not.toBeInTheDocument();
    expect(document.querySelector('a[href="/activity"]')).toBeInTheDocument();
  });

  it("reconciles once on SSE open without polling while connected", () => {
    const { invalidateQueries } = renderShell();
    const source = currentSource();
    const accessChanged = vi.fn();
    window.addEventListener("pulse:access-changed", accessChanged);

    act(() => source.emit("open"));
    act(() => vi.advanceTimersByTime(250));
    expect(invalidateQueries).toHaveBeenCalledOnce();
    expect(accessChanged).toHaveBeenCalledOnce();
    expect(refreshSessionMock).toHaveBeenCalledWith();

    act(() => vi.advanceTimersByTime(60_000));
    expect(invalidateQueries).toHaveBeenCalledOnce();
    window.removeEventListener("pulse:access-changed", accessChanged);
  });

  it("polls only after a connection error and stops again after recovery", () => {
    const { invalidateQueries } = renderShell();
    const source = currentSource();
    const accessChanged = vi.fn();
    window.addEventListener("pulse:access-changed", accessChanged);

    act(() => source.emit("open"));
    act(() => vi.advanceTimersByTime(250));
    expect(accessChanged).toHaveBeenCalledTimes(1);
    act(() => source.emit("error"));
    act(() => vi.advanceTimersByTime(15_250));
    expect(invalidateQueries).toHaveBeenCalledTimes(2);
    expect(accessChanged).toHaveBeenCalledTimes(2);

    act(() => source.emit("open"));
    act(() => vi.advanceTimersByTime(250));
    act(() => vi.advanceTimersByTime(45_000));
    expect(invalidateQueries).toHaveBeenCalledTimes(3);
    expect(accessChanged).toHaveBeenCalledTimes(3);
    expect(refreshSessionMock).toHaveBeenCalledWith({ failClosed: true });
    window.removeEventListener("pulse:access-changed", accessChanged);
  });

  it("coalesces a burst of domain events into one refresh", () => {
    const { invalidateQueries } = renderShell();
    const source = currentSource();
    const pulseRefresh = vi.fn();
    window.addEventListener("pulse:refresh", pulseRefresh);

    act(() => source.emit("open"));
    act(() => {
      source.emit("deal.updated");
      vi.advanceTimersByTime(100);
      source.emit("task.updated");
      vi.advanceTimersByTime(100);
      source.emit("task.deleted");
      source.emit("deal.deleted");
      source.emit("contact.deleted");
      vi.advanceTimersByTime(49);
    });
    expect(invalidateQueries).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1));
    expect(invalidateQueries).toHaveBeenCalledOnce();
    expect(pulseRefresh).toHaveBeenCalledOnce();

    window.removeEventListener("pulse:refresh", pulseRefresh);
  });

  it("purges CRM cache and publishes a dedicated access change event", () => {
    const { invalidateQueries, queryClient } = renderShell();
    const source = currentSource();
    const accessChanged = vi.fn();
    queryClient.setQueryData(["tasks", "all"], { items: [{ id: "task-private" }] });
    queryClient.setQueryData(["push", "subscription"], { enabled: true });
    window.addEventListener("pulse:access-changed", accessChanged);

    act(() => source.emit("access.changed"));

    expect(queryClient.getQueryData(["tasks", "all"])).toBeUndefined();
    expect(queryClient.getQueryData(["push", "subscription"])).toEqual({ enabled: true });
    expect(accessChanged).toHaveBeenCalledOnce();
    expect(refreshSessionMock).toHaveBeenCalledWith({ failClosed: true });
    expect(invalidateQueries).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(250));
    expect(invalidateQueries).not.toHaveBeenCalled();
    window.removeEventListener("pulse:access-changed", accessChanged);
  });

  it("probes the current session after an SSE failure", () => {
    renderShell();
    const source = currentSource();

    act(() => source.emit("error"));

    expect(refreshSessionMock).toHaveBeenCalledWith();
  });

  it("clears pending refreshes and listeners on unmount", () => {
    const { invalidateQueries, unmount } = renderShell();
    const source = currentSource();

    act(() => source.emit("deal.updated"));
    unmount();
    act(() => vi.advanceTimersByTime(60_000));

    expect(invalidateQueries).not.toHaveBeenCalled();
    expect(source.close).toHaveBeenCalledOnce();
    expect(source.listenerCount("open")).toBe(0);
    expect(source.listenerCount("access.changed")).toBe(0);
    expect(source.listenerCount("deal.updated")).toBe(0);
  });
});
