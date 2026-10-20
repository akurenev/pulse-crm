import type { MailAccount, MailCompose, MailFolder, MailMessage } from "../../types/mail";

const stamp = "2026-10-20T08:00:00Z";
export const demoMailAccount: MailAccount = {
  id: "demo-mail", email: "employee@example.com", display_name: "Рабочая почта", username: "employee@example.com",
  imap_host: "imap.example.com", imap_port: 993, imap_security: "ssl", smtp_host: "smtp.example.com", smtp_port: 587, smtp_security: "starttls",
  status: "active", sync_days: 30, last_synced_at: stamp, last_error: null, has_credentials: false, version: 1, created_at: stamp, updated_at: stamp,
};
export const demoMailFolders: MailFolder[] = [
  ["inbox", "Входящие"], ["sent", "Отправленные"], ["drafts", "Черновики"], ["archive", "Архив"], ["trash", "Корзина"],
].map(([kind, display_name]) => ({ id: `demo-${kind}`, account_id: "demo-mail", name: kind, display_name, kind: kind as MailFolder["kind"], unread_count: kind === "inbox" ? 1 : 0, total_count: kind === "inbox" ? 2 : 0 }));

export function demoDraft(account: MailAccount, compose: MailCompose, id: string = crypto.randomUUID()): MailMessage {
  return {
    id, account_id: account.id, folder_id: "demo-drafts", subject: compose.subject,
    from_address: account.email, from_name: account.display_name, to_addresses: compose.to_addresses,
    cc_addresses: compose.cc_addresses, bcc_addresses: compose.bcc_addresses, body_text: compose.body_text,
    body_html: null, internet_message_id: null, in_reply_to: null, references: [], thread_key: id,
    direction: "outbound", status: "draft", is_read: true, is_starred: false,
    sent_at: null, received_at: null, created_at: stamp, updated_at: stamp, version: 1,
    last_error: null, attachments: [], linked_deal_ids: [],
  };
}

export const demoMailMessages: MailMessage[] = [
  { ...demoDraft(demoMailAccount, { subject: "Согласование предложения", to_addresses: [demoMailAccount.email], cc_addresses: [], bcc_addresses: [], body_text: "Добрый день!\n\nСпасибо за предложение. Подскажите, пожалуйста, возможную дату поставки и условия оплаты.\n\nС уважением,\nТестовый клиент" }, "demo-incoming-1"), folder_id: "demo-inbox", direction: "inbound", status: "received", from_address: "client@example.com", from_name: "Тестовый клиент", is_read: false, is_starred: true, received_at: stamp },
  { ...demoDraft(demoMailAccount, { subject: "Встреча по проекту", to_addresses: [demoMailAccount.email], cc_addresses: ["colleague@example.com"], bcc_addresses: [], body_text: "Коллеги, добрый день!\n\nПредлагаю обсудить детали проекта завтра в 11:00. Будет ли удобно?\n\nЭто демонстрационное письмо." }, "demo-incoming-2"), folder_id: "demo-inbox", direction: "inbound", status: "received", from_address: "partner@example.com", from_name: "Тестовый партнёр", received_at: stamp },
];
