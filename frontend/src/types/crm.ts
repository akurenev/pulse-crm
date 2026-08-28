export type SourceCode =
  | "manual"
  | "email"
  | "telegram"
  | "max"
  | "webhook"
  | "html_form"
  | "amo_import";

export type DealStatus = "open" | "won" | "lost";

export interface UserSummary {
  id: string;
  name: string;
  initials: string;
  tone: "blue" | "violet" | "green" | "amber";
}

export interface Message {
  id: string;
  direction: "inbound" | "outbound";
  body: string;
  createdAt: string;
  status: "received" | "queued" | "sent" | "failed";
  lastError?: string;
}

export interface DealTask {
  id: string;
  title: string;
  dueAt: string;
  completed: boolean;
  assignee: UserSummary;
  version: number;
}

export interface Deal {
  id: string;
  title: string;
  subtitle: string;
  amount: number;
  currency: "RUB";
  source: SourceCode;
  sourceLabel: string;
  assignee: UserSummary;
  dueDate: string;
  stageId: string;
  status: DealStatus;
  contactIds?: string[];
  contactName?: string;
  phone?: string;
  email?: string;
  companyId?: string;
  companyName?: string;
  customFields?: Record<string, unknown>;
  nextPurchaseAt?: string;
  version: number;
  messages: Message[];
  tasks: DealTask[];
}

export interface Stage {
  id: string;
  name: string;
  color: "blue" | "violet" | "green" | "amber";
  stageType?: "open" | "won" | "lost";
}

export interface Pipeline {
  id: string;
  name: string;
  stages: Stage[];
}

export interface Contact {
  id: string;
  name: string;
  company: string;
  email: string;
  phone: string;
  deals: number;
  revenue: number;
  nextPurchaseAt?: string;
  assignee: UserSummary;
}

export interface TaskItem {
  id: string;
  title: string;
  entity: string;
  dueAt: string;
  status: "today" | "overdue" | "upcoming" | "done";
  assignee: UserSummary;
}

export interface ActivityItem {
  id: string;
  kind: "deal" | "message" | "task" | "contact" | "system";
  title: string;
  detail: string;
  createdAt: string;
  actor: UserSummary;
}
