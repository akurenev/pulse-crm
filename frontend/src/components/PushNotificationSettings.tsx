import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellOff, Check, LoaderCircle, Send } from "lucide-react";
import { useState } from "react";

import { api, ApiError, remoteEnabled } from "../lib/api";
import {
  disableWebPush,
  enableWebPush,
  getCurrentPushSubscription,
  getWebPushSupport,
  isIosDevice,
  isStandalonePwa,
  sendTestWebPush,
  syncCurrentWebPush,
} from "../lib/web-push";
import type { ApiPushConfig } from "../types/api";

const PUSH_CONFIG_QUERY_KEY = ["push", "config"] as const;

type PushAction = "idle" | "enable" | "disable" | "test";

export function PushNotificationSettings() {
  const queryClient = useQueryClient();
  const support = getWebPushSupport();
  const ios = isIosDevice();
  const standalone = isStandalonePwa();
  const [permission, setPermission] = useState<NotificationPermission>(() => (
    typeof Notification === "undefined" ? "default" : Notification.permission
  ));
  const [action, setAction] = useState<PushAction>("idle");
  const [feedback, setFeedback] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const configQuery = useQuery({
    queryKey: PUSH_CONFIG_QUERY_KEY,
    queryFn: () => api.get<ApiPushConfig>("/push/config"),
    enabled: remoteEnabled,
    staleTime: 60_000,
  });
  const config = configQuery.data;
  const configReady = config?.enabled === true && Boolean(config.public_key);
  const subscriptionQueryKey = ["push", "subscription", configReady ? config?.public_key : null] as const;
  const subscriptionQuery = useQuery({
    queryKey: subscriptionQueryKey,
    queryFn: async () => (
      configReady && config?.public_key
        ? syncCurrentWebPush(config.public_key)
        : Boolean(await getCurrentPushSubscription())
    ),
    enabled: remoteEnabled
      && support === "supported"
      && (configQuery.isSuccess || configQuery.isError),
  });

  const subscribed = subscriptionQuery.data === true;
  const busy = action !== "idle";
  const canEnable = configReady && support === "supported" && permission !== "denied" && (!ios || standalone);

  async function handleEnable() {
    if (!config?.public_key || busy) return;
    setAction("enable");
    setFeedback(null);
    setActionError(null);
    try {
      await enableWebPush(config.public_key);
      setPermission(Notification.permission);
      queryClient.setQueryData(subscriptionQueryKey, true);
      setFeedback("Настройка сохранена");
    } catch {
      setPermission(Notification.permission);
      if (Notification.permission !== "denied") setActionError("Не удалось включить push-уведомления. Попробуйте ещё раз.");
    } finally {
      setAction("idle");
    }
  }

  async function handleDisable() {
    if (busy) return;
    setAction("disable");
    setFeedback(null);
    setActionError(null);
    try {
      await disableWebPush();
      queryClient.setQueryData(subscriptionQueryKey, false);
      setFeedback("Настройка сохранена");
    } catch {
      const remainsSubscribed = Boolean(await getCurrentPushSubscription().catch(() => null));
      queryClient.setQueryData(subscriptionQueryKey, remainsSubscribed);
      setActionError(remainsSubscribed
        ? "Не удалось отключить push-уведомления. Попробуйте ещё раз."
        : "Подписка удалена из браузера, но сервер не подтвердил отключение.");
    } finally {
      setAction("idle");
    }
  }

  async function handleTest() {
    if (busy || !subscribed) return;
    setAction("test");
    setFeedback(null);
    setActionError(null);
    try {
      await sendTestWebPush();
      setFeedback("Тестовое уведомление отправлено");
    } catch (reason) {
      setActionError(
        reason instanceof ApiError && reason.status === 429
          ? "Тест уже отправлен. Повторите через минуту."
          : "Не удалось отправить тестовое уведомление.",
      );
    } finally {
      setAction("idle");
    }
  }

  let status = "Push-уведомления выключены";
  let statusTone = "off";
  let hint = "Включите, чтобы получать напоминания даже при закрытой CRM.";

  if (!remoteEnabled) {
    status = "Push-уведомления недоступны в демо-режиме";
    statusTone = "off";
    hint = "Подключите CRM к серверу, чтобы настроить уведомления.";
  } else if (configQuery.isLoading) {
    status = "Проверяем push-уведомления";
    statusTone = "loading";
    hint = "Настройка займёт несколько секунд.";
  } else if (configQuery.isError) {
    status = "Не удалось проверить push-уведомления";
    statusTone = "error";
    hint = "Обновите страницу или попробуйте позже.";
  } else if (!configReady) {
    status = "Push-уведомления отключены на сервере";
    statusTone = "off";
    hint = "Обратитесь к администратору CRM.";
  } else if (support === "unsupported") {
    status = "Push-уведомления недоступны";
    statusTone = "off";
    hint = ios
      ? "На iPhone установите CRM на экран «Домой» и откройте её как приложение. Требуется iOS 16.4 или новее."
      : "Этот браузер не поддерживает push-уведомления.";
  } else if (ios && !standalone) {
    status = "Установите CRM на экран «Домой»";
    statusTone = "off";
    hint = "На iPhone push работают только в установленном PWA: «Поделиться» → «На экран Домой».";
  } else if (permission === "denied") {
    status = "Уведомления заблокированы";
    statusTone = "error";
    hint = "Разрешите уведомления для Pulse CRM в настройках браузера или системы.";
  } else if (subscriptionQuery.isError) {
    status = "Не удалось синхронизировать подписку";
    statusTone = "error";
    hint = "Нажмите «Включить», чтобы восстановить push-уведомления.";
  } else if (subscriptionQuery.isLoading) {
    status = "Проверяем подписку";
    statusTone = "loading";
    hint = "Настройка займёт несколько секунд.";
  } else if (subscribed) {
    status = "Push-уведомления включены";
    statusTone = "on";
    hint = "Напоминания будут приходить на это устройство.";
  }

  return (
    <section className="push-settings" aria-label="Настройка push-уведомлений">
      <div className={`push-settings__status push-settings__status--${statusTone}`}>
        {statusTone === "loading" ? <LoaderCircle size={15} className="push-settings__spinner" aria-hidden="true" /> : null}
        {statusTone === "on" ? <Check size={15} aria-hidden="true" /> : null}
        {statusTone === "off" || statusTone === "error" ? <BellOff size={15} aria-hidden="true" /> : null}
        <strong>{status}</strong>
      </div>
      <p>{hint}</p>
      {ios && standalone ? <small className="push-settings__ios">Push поддерживаются в PWA на iOS 16.4 и новее.</small> : null}
      {actionError ? <small className="push-settings__error" role="alert">{actionError}</small> : null}
      {feedback ? <small className="push-settings__success" role="status">{feedback}</small> : null}
      <div className="push-settings__actions">
        {canEnable && !subscribed ? (
          <button type="button" onClick={() => void handleEnable()} disabled={busy || subscriptionQuery.isLoading}>
            {action === "enable" ? "Включаем…" : "Включить"}
          </button>
        ) : null}
        {support === "supported" && subscribed ? (
          <button type="button" onClick={() => void handleDisable()} disabled={busy}>
            {action === "disable" ? "Выключаем…" : "Выключить"}
          </button>
        ) : null}
        {configReady && support === "supported" && subscribed ? (
          <button type="button" className="push-settings__test" onClick={() => void handleTest()} disabled={busy}>
            <Send size={13} aria-hidden="true" />
            {action === "test" ? "Отправляем…" : "Проверить"}
          </button>
        ) : null}
      </div>
    </section>
  );
}
