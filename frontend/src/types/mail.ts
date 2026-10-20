export interface MailAccount {
  id: string;
  email: string;
  display_name: string;
  username: string;
  imap_host: string;
  imap_port: number;
  imap_security: "ssl" | "starttls";
  smtp_host: string;
  smtp_port: number;
  smtp_security: "ssl" | "starttls";
  status: "active" | "disabled" | "degraded";
  sync_days: number;
  last_synced_at: string | null;
  last_error: string | null;
  has_credentials: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface MailFolder {
  id: string;
  account_id: string;
  name: string;
  display_name: string;
  kind: "inbox" | "sent" | "drafts" | "outbox" | "trash" | "archive" | "spam" | "custom";
  unread_count: number;
  total_count: number;
}

export interface MailAttachment {
  id: string;
  message_id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
}

export interface MailMessage {
  id: string;
  account_id: string;
  folder_id: string;
  subject: string;
  from_address: string;
  from_name: string | null;
  reply_to_addresses?: string[];
  to_addresses: string[];
  cc_addresses: string[];
  bcc_addresses: string[];
  body_text: string;
  body_html: string | null;
  internet_message_id: string | null;
  in_reply_to: string | null;
  references: string[];
  thread_key: string;
  direction: "inbound" | "outbound";
  status: "received" | "draft" | "queued" | "sending" | "sent" | "failed" | "uncertain";
  is_read: boolean;
  is_starred: boolean;
  sent_at: string | null;
  received_at: string | null;
  created_at: string;
  updated_at: string;
  version: number;
  last_error: string | null;
  attachments: MailAttachment[];
  linked_deal_ids: string[];
}

export interface MailCompose {
  subject: string;
  to_addresses: string[];
  cc_addresses: string[];
  bcc_addresses: string[];
  body_text: string;
  reply_to_message_id?: string | null;
  forward_message_id?: string | null;
}

export interface MailAccountInput {
  email: string;
  display_name: string;
  username: string;
  password: string;
  imap_host: string;
  imap_port: number;
  imap_security: "ssl" | "starttls";
  smtp_host: string;
  smtp_port: number;
  smtp_security: "ssl" | "starttls";
  sync_days: number;
}
