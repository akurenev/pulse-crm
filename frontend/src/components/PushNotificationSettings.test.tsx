import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PushNotificationSettings } from "./PushNotificationSettings";
import { ApiError } from "../lib/api";

const {
  apiGetMock,
  disableMock,
  enableMock,
  getCurrentMock,
  iosMock,
  sendTestMock,
  standaloneMock,
  supportMock,
  syncMock,
} = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  disableMock: vi.fn(),
  enableMock: vi.fn(),
  getCurrentMock: vi.fn(),
  iosMock: vi.fn(),
  sendTestMock: vi.fn(),
  standaloneMock: vi.fn(),
  supportMock: vi.fn(),
  syncMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number) {
      super(message);
    }
  },
  api: { get: apiGetMock },
  remoteEnabled: true,
}));

vi.mock("../lib/web-push", () => ({
  disableWebPush: disableMock,
  enableWebPush: enableMock,
  getCurrentPushSubscription: getCurrentMock,
  getWebPushSupport: supportMock,
  isIosDevice: iosMock,
  isStandalonePwa: standaloneMock,
  sendTestWebPush: sendTestMock,
  syncCurrentWebPush: syncMock,
}));

const originalNotification = Object.getOwnPropertyDescriptor(globalThis, "Notification");

function Wrapper({ children }: PropsWithChildren) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

function renderSettings() {
  return render(<PushNotificationSettings />, { wrapper: Wrapper });
}

beforeEach(() => {
  apiGetMock.mockReset().mockResolvedValue({ enabled: true, public_key: "AQID-_8" });
  disableMock.mockReset().mockResolvedValue(undefined);
  enableMock.mockReset().mockResolvedValue({});
  getCurrentMock.mockReset().mockResolvedValue(null);
  iosMock.mockReset().mockReturnValue(false);
  sendTestMock.mockReset().mockResolvedValue(undefined);
  standaloneMock.mockReset().mockReturnValue(false);
  supportMock.mockReset().mockReturnValue("supported");
  syncMock.mockReset().mockResolvedValue(false);
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: { permission: "default" },
  });
});

afterEach(() => {
  cleanup();
  if (originalNotification) Object.defineProperty(globalThis, "Notification", originalNotification);
  else Reflect.deleteProperty(globalThis, "Notification");
});

describe("PushNotificationSettings", () => {
  it("enables push only after a user click and can send a test", async () => {
    const user = userEvent.setup();
    renderSettings();

    expect(enableMock).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Включить" }));

    expect(enableMock).toHaveBeenCalledWith("AQID-_8");
    expect(await screen.findByText("Push-уведомления включены")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Проверить" }));
    expect(sendTestMock).toHaveBeenCalledOnce();
    expect(await screen.findByText("Тестовое уведомление отправлено")).toBeInTheDocument();
  });

  it("shows an existing re-synced subscription and disables it", async () => {
    const user = userEvent.setup();
    syncMock.mockResolvedValue(true);
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "Выключить" }));

    expect(disableMock).toHaveBeenCalledOnce();
    expect(await screen.findByText("Push-уведомления выключены")).toBeInTheDocument();
  });

  it("explains the one-minute test cooldown", async () => {
    const user = userEvent.setup();
    syncMock.mockResolvedValue(true);
    sendTestMock.mockRejectedValueOnce(new ApiError("rate limited", 429));
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "Проверить" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Тест уже отправлен. Повторите через минуту.",
    );
  });

  it("explains a browser-level permission denial", async () => {
    Object.defineProperty(globalThis, "Notification", {
      configurable: true,
      value: { permission: "denied" },
    });
    renderSettings();

    expect(await screen.findByText("Уведомления заблокированы")).toBeInTheDocument();
    expect(screen.getByText(/Разрешите уведомления/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Включить" })).not.toBeInTheDocument();
  });

  it("does not offer activation in an iPhone browser tab", async () => {
    iosMock.mockReturnValue(true);
    standaloneMock.mockReturnValue(false);
    renderSettings();

    expect(await screen.findByText("Установите CRM на экран «Домой»")).toBeInTheDocument();
    expect(screen.getByText(/«Поделиться» → «На экран Домой»/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Включить" })).not.toBeInTheDocument();
  });

  it("shows when Web Push is disabled on the server", async () => {
    apiGetMock.mockResolvedValue({ enabled: false, public_key: null });
    renderSettings();

    expect(await screen.findByText("Push-уведомления отключены на сервере")).toBeInTheDocument();
    expect(screen.getByText("Обратитесь к администратору CRM.")).toBeInTheDocument();
    await waitFor(() => expect(syncMock).not.toHaveBeenCalled());
  });

  it("still lets the user remove a local subscription when the server disables Web Push", async () => {
    const user = userEvent.setup();
    apiGetMock.mockResolvedValue({ enabled: false, public_key: null });
    getCurrentMock.mockResolvedValue({ endpoint: "https://push.example.test/subscription" });
    renderSettings();

    await user.click(await screen.findByRole("button", { name: "Выключить" }));

    expect(disableMock).toHaveBeenCalledOnce();
  });
});
