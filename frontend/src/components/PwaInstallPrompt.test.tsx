import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PwaInstallPrompt } from "./PwaInstallPrompt";

const originalUserAgent = window.navigator.userAgent;
const originalMaxTouchPoints = window.navigator.maxTouchPoints;
const originalLocalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");

function mockLocalStorage() {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
}

function setNavigator(userAgent: string, maxTouchPoints = 0) {
  Object.defineProperty(window.navigator, "userAgent", { configurable: true, value: userAgent });
  Object.defineProperty(window.navigator, "maxTouchPoints", { configurable: true, value: maxTouchPoints });
}

function installEvent(outcome: "accepted" | "dismissed" = "accepted") {
  const event = new Event("beforeinstallprompt", { cancelable: true }) as Event & {
    prompt: ReturnType<typeof vi.fn>;
    userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
  };
  event.prompt = vi.fn().mockResolvedValue(undefined);
  event.userChoice = Promise.resolve({ outcome, platform: "web" });
  return event;
}

beforeEach(() => {
  mockLocalStorage();
  setNavigator(originalUserAgent, originalMaxTouchPoints);
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

afterEach(() => {
  cleanup();
  setNavigator(originalUserAgent, originalMaxTouchPoints);
  if (originalLocalStorage) Object.defineProperty(window, "localStorage", originalLocalStorage);
  vi.restoreAllMocks();
});

describe("PwaInstallPrompt", () => {
  it("opens the browser installation prompt and disappears after acceptance", async () => {
    const user = userEvent.setup();
    const event = installEvent();
    render(<PwaInstallPrompt />);

    act(() => window.dispatchEvent(event));
    await user.click(screen.getByRole("button", { name: "Установить" }));

    expect(event.prompt).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByText("Установить Pulse CRM")).not.toBeInTheDocument());
  });

  it("shows manual Add to Home Screen guidance on iOS", async () => {
    setNavigator("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", 5);
    const user = userEvent.setup();
    render(<PwaInstallPrompt />);

    expect(screen.getByText(/На экран Домой/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Понятно" }));

    expect(screen.queryByText("Установить Pulse CRM")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("pulse:pwa-install-dismissed-at")).not.toBeNull();
  });

  it("stays hidden when the app already runs in standalone mode", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({ matches: true }),
    });

    render(<PwaInstallPrompt />);
    act(() => window.dispatchEvent(installEvent()));

    expect(screen.queryByText("Установить Pulse CRM")).not.toBeInTheDocument();
  });

  it("hides an open offer after the browser reports installation", () => {
    render(<PwaInstallPrompt />);
    act(() => window.dispatchEvent(installEvent()));
    expect(screen.getByText("Установить Pulse CRM")).toBeInTheDocument();

    act(() => window.dispatchEvent(new Event("appinstalled")));

    expect(screen.queryByText("Установить Pulse CRM")).not.toBeInTheDocument();
  });
});
