import { Download, Share2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "./Button";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

const DISMISSED_AT_KEY = "pulse:pwa-install-dismissed-at";
const DISMISSAL_TTL_MS = 30 * 24 * 60 * 60 * 1000;

function isStandalone() {
  return window.matchMedia?.("(display-mode: standalone)").matches
    || Boolean((navigator as Navigator & { standalone?: boolean }).standalone);
}

function isIosDevice() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.userAgent.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

function wasRecentlyDismissed() {
  try {
    const dismissedAt = Number(window.localStorage.getItem(DISMISSED_AT_KEY));
    return Number.isFinite(dismissedAt) && dismissedAt > 0 && Date.now() - dismissedAt < DISMISSAL_TTL_MS;
  } catch {
    return false;
  }
}

function rememberDismissal() {
  try {
    window.localStorage.setItem(DISMISSED_AT_KEY, String(Date.now()));
  } catch {
    // The prompt can still be hidden for the current page when storage is unavailable.
  }
}

export function PwaInstallPrompt({ enabled = true }: { enabled?: boolean }) {
  const [deferredPrompt, setDeferredPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [dismissed, setDismissed] = useState(() => wasRecentlyDismissed());
  const [installed, setInstalled] = useState(() => isStandalone());
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState(false);
  const ios = isIosDevice();

  useEffect(() => {
    const handleBeforeInstallPrompt = (event: Event) => {
      event.preventDefault();
      if (!wasRecentlyDismissed()) {
        setDismissed(false);
        setDeferredPrompt(event as BeforeInstallPromptEvent);
      }
    };
    const handleInstalled = () => {
      setInstalled(true);
      setDeferredPrompt(null);
      try {
        window.localStorage.removeItem(DISMISSED_AT_KEY);
      } catch {
        // Installation is complete even if storage is unavailable.
      }
    };

    window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
    window.addEventListener("appinstalled", handleInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
      window.removeEventListener("appinstalled", handleInstalled);
    };
  }, []);

  const dismiss = () => {
    rememberDismissal();
    setDismissed(true);
    setDeferredPrompt(null);
  };

  const install = async () => {
    if (!deferredPrompt) return;
    setInstalling(true);
    setInstallError(false);
    try {
      await deferredPrompt.prompt();
      const choice = await deferredPrompt.userChoice;
      if (choice.outcome === "accepted") {
        setInstalled(true);
        setDeferredPrompt(null);
      } else {
        dismiss();
      }
    } catch {
      setInstallError(true);
    } finally {
      setInstalling(false);
    }
  };

  const visible = enabled && !installed && !dismissed && (ios || deferredPrompt !== null);
  if (!visible) return null;

  return (
    <section className="pwa-install-prompt" aria-labelledby="pwa-install-title" aria-live="polite">
      <span className="pwa-install-prompt__icon" aria-hidden="true">
        {ios ? <Share2 size={24} /> : <Download size={24} />}
      </span>
      <div className="pwa-install-prompt__copy">
        <strong id="pwa-install-title">Установить Pulse CRM</strong>
        <p>
          {ios
            ? "Откройте «Поделиться» и выберите «На экран Домой»."
            : "Открывайте CRM с главного экрана — как обычное приложение."}
        </p>
        {installError ? <small role="alert">Не удалось открыть установку. Используйте меню браузера.</small> : null}
      </div>
      <button className="pwa-install-prompt__close" type="button" aria-label="Закрыть предложение установки" onClick={dismiss}>
        <X size={18} aria-hidden="true" />
      </button>
      <div className="pwa-install-prompt__actions">
        {deferredPrompt ? (
          <Button type="button" variant="primary" compact disabled={installing} onClick={() => void install()}>
            {installing ? "Открываем…" : "Установить"}
          </Button>
        ) : null}
        <Button type="button" variant="ghost" compact onClick={dismiss}>{ios ? "Понятно" : "Не сейчас"}</Button>
      </div>
    </section>
  );
}
