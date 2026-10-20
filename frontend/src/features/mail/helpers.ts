import { api, ApiError } from "../../lib/api";
import type { MailAccount, MailAttachment, MailCompose, MailMessage } from "../../types/mail";

export const mailStatusLabels: Record<MailMessage["status"], string> = {
  received: "Получено", draft: "Черновик", queued: "В очереди", sending: "Отправляется",
  sent: "Отправлено", failed: "Ошибка отправки", uncertain: "Доставка не подтверждена",
};

export function mailError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "Письмо или ящик уже изменены. Данные обновлены; проверьте их и повторите действие. Текст редактора сохранён.";
    if (error.status === 413) return "Файл слишком большой. Уменьшите его размер и повторите загрузку.";
    if (error.status === 415) return "Этот тип вложения не поддерживается.";
    if (error.status === 422) return "Проверьте адреса, параметры сервера и размер вложений.";
    if (error.status === 404 || error.status === 403) return "Письмо, ящик или сделка больше недоступны. Обновите страницу.";
    if (error.status === 429) return "Лимит запросов временно исчерпан. Повторите позже.";
    if (error.status === 401) return "Войдите в CRM повторно.";
  }
  return "Не удалось выполнить действие. Проверьте подключение и повторите попытку.";
}

export function parseAddresses(value: string): string[] {
  return [...new Set(value.split(/[,;\n]+/).map((part) => part.trim()).filter(Boolean))];
}

export function validAddresses(addresses: string[]): boolean {
  return addresses.every((address) => /^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$/.test(address));
}

export function quoteMessage(message: MailMessage): string {
  return `\n\n──── Исходное письмо ────\nОт: ${message.from_address}\nКому: ${message.to_addresses.join(", ")}\nТема: ${message.subject}\n\n${message.body_text}`;
}

export function composeFromMessage(message: MailMessage, account: MailAccount, mode: "reply" | "reply-all" | "forward" | "edit"): MailCompose {
  if (mode === "edit") return {
    subject: message.subject, to_addresses: message.to_addresses, cc_addresses: message.cc_addresses,
    bcc_addresses: message.bcc_addresses, body_text: message.body_text,
  };
  const self = account.email.toLowerCase();
  const otherAddresses = (addresses: string[]) => {
    const seen = new Set([self]);
    return addresses.filter((address) => { const key = address.toLowerCase(); if (seen.has(key)) return false; seen.add(key); return true; });
  };
  const replyAddresses = message.from_address.toLowerCase() === self ? message.to_addresses : message.reply_to_addresses?.length ? message.reply_to_addresses : [message.from_address];
  const to = otherAddresses([...replyAddresses, ...(mode === "reply-all" ? message.to_addresses : [])]);
  const toKeys = new Set(to.map((address) => address.toLowerCase()));
  const quote = quoteMessage(message);
  const truncationNotice = "\n\n[Цитата сокращена до лимита редактора. Полный текст доступен в исходном письме.]";
  return {
    subject: `${mode === "forward" ? "Fwd" : "Re"}: ${message.subject.replace(/^(re|fwd):\s*/i, "")}`.slice(0, 998),
    to_addresses: mode === "forward" ? [] : to,
    cc_addresses: mode === "reply-all" ? otherAddresses(message.cc_addresses).filter((address) => !toKeys.has(address.toLowerCase())) : [],
    bcc_addresses: [], body_text: quote.length > 100_000 ? quote.slice(0, 100_000 - truncationNotice.length) + truncationNotice : quote,
    reply_to_message_id: mode === "forward" ? null : message.id,
    forward_message_id: mode === "forward" ? message.id : null,
  };
}

export function messageDate(message: MailMessage): string {
  const value = message.received_at ?? message.sent_at ?? message.created_at;
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export function fileSize(size: number): string {
  return size < 1024 ? `${size} Б` : size < 1024 * 1024 ? `${Math.ceil(size / 1024)} КБ` : `${(size / 1024 / 1024).toFixed(1)} МБ`;
}

export async function downloadMailAttachment(attachment: MailAttachment) {
  const result = await api.post<{ url: string; expires_in: number }>(`/mail/attachments/${attachment.id}/download`, {});
  const parsed = new URL(result.url, window.location.origin);
  if (!["https:", "http:"].includes(parsed.protocol)) throw new Error("Invalid download URL");
  const link = document.createElement("a"); link.href = parsed.href; link.rel = "noopener noreferrer"; link.target = "_blank"; link.download = attachment.original_filename; link.click();
}
