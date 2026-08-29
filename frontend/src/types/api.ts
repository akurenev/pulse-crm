export interface ApiWorkspace {
  id: string;
  name: string;
  slug: string;
  timezone: string;
  currency: string;
}

export interface ApiUser {
  id: string;
  email: string;
  full_name: string;
  role: "owner" | "admin" | "manager" | null;
}

export interface AuthResponse {
  user: ApiUser;
  workspace: ApiWorkspace;
  csrf_token: string;
}

export interface InvitationCreated {
  id: string;
  email: string;
  role: "admin" | "manager";
  expires_at: string;
  token: string;
}

export interface ApiStage {
  id: string;
  pipeline_id: string;
  name: string;
  color: string;
  position: number;
  stage_type: "open" | "won" | "lost";
  version: number;
}

export interface ApiPipeline {
  id: string;
  name: string;
  position: number;
  is_active: boolean;
  version: number;
  stages: ApiStage[];
}

export type ApiCustomFieldEntity = "deal" | "contact" | "company";
export type ApiCustomFieldType = "text" | "number" | "date" | "boolean" | "select";

export interface ApiCustomField {
  id: string;
  entity_type: ApiCustomFieldEntity;
  key: string;
  name: string;
  field_type: ApiCustomFieldType;
  options: string[];
  is_active: boolean;
}

export interface ApiRequiredField {
  id: string;
  field_definition_id: string | null;
  built_in_key: string | null;
}

export interface ApiSource {
  id: string;
  key: string;
  name: string;
  is_active: boolean;
}

export interface ApiDeal {
  id: string;
  title: string;
  pipeline_id: string;
  stage_id: string;
  company_id: string | null;
  company: ApiCompany | null;
  contact_ids: string[];
  primary_contact: ApiContact | null;
  assignee_id: string | null;
  source_id: string | null;
  amount: string | number | null;
  currency: string;
  custom_fields: Record<string, unknown>;
  next_purchase_at: string | null;
  last_activity_at: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiTask {
  id: string;
  title: string;
  description: string | null;
  task_type: string;
  status: "open" | "completed" | "cancelled";
  due_at: string;
  remind_at: string | null;
  assignee_id: string;
  deal_id: string | null;
  contact_id: string | null;
  company_id: string | null;
  completed_at: string | null;
  version: number;
}

export interface ApiContact {
  id: string;
  first_name: string;
  last_name: string;
  company_id: string | null;
  primary_email: string | null;
  primary_phone: string | null;
  emails: string[];
  phones: string[];
  tags: string[];
  custom_fields: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiCompany {
  id: string;
  name: string;
  website: string | null;
  phone: string | null;
  email: string | null;
  tags: string[];
  custom_fields: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiActivity {
  id: string;
  event_type: string;
  entity_type: string;
  entity_id: string;
  actor_id: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface ApiMessage {
  id: string;
  conversation_id: string;
  direction: "inbound" | "outbound";
  status: "received" | "queued" | "sent" | "failed";
  body: string;
  channel: "email" | "telegram" | "max" | "internal";
  provider_message_id: string | null;
  created_at: string;
  sent_at: string | null;
  received_at: string | null;
  last_error: string | null;
}

export interface ApiAttachment {
  id: string;
  message_id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export interface ApiMessageWithAttachment {
  message: ApiMessage;
  attachment: ApiAttachment;
}

export interface ApiPipelineConversion {
  pipeline_id: string;
  pipeline_name: string;
  total_deals: number;
  won_deals: number;
  conversion_percent: number;
}

export interface ApiDashboard {
  new_leads_24h: number;
  overdue_tasks: number;
  inactive_deals: number;
  upcoming_purchases_30d: number;
  pipelines: ApiPipelineConversion[];
}

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export type ApiChannelKind = "email" | "telegram" | "max";
export type ApiConnectionStatus = "disabled" | "active" | "degraded";

export interface ApiChannelConnection {
  id: string;
  kind: ApiChannelKind;
  name: string;
  status: ApiConnectionStatus;
  settings: Record<string, unknown>;
  default_pipeline_id: string | null;
  default_stage_id: string | null;
  default_assignee_id: string | null;
  has_credentials: boolean;
  last_healthcheck_at: string | null;
  last_error: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiHtmlForm {
  id: string;
  slug: string;
  title: string;
  pipeline_id: string;
  stage_id: string;
  assignee_id: string | null;
  source_id: string | null;
  fields_schema: Record<string, unknown>[];
  allowed_origins: string[];
  honeypot_field: string;
  success_message: string;
  is_active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiWebhookEndpoint {
  id: string;
  slug: string;
  name: string;
  pipeline_id: string;
  stage_id: string;
  assignee_id: string | null;
  source_id: string | null;
  is_active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiWebhookEndpointCreated extends ApiWebhookEndpoint {
  secret: string;
}

export type ApiNotificationAudience = "employee" | "client";
export type ApiNotificationChannel = "in_app" | "email" | "telegram" | "max";

export interface ApiNotificationTemplate {
  id: string;
  name: string;
  channel: ApiNotificationChannel;
  subject_template: string | null;
  body_template: string;
  is_active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiNotificationRule {
  id: string;
  template_id: string;
  name: string;
  event_type: string;
  audience: ApiNotificationAudience;
  channel: ApiNotificationChannel;
  pipeline_id: string | null;
  stage_id: string | null;
  source_id: string | null;
  filters: Record<string, unknown>;
  recipients: Record<string, unknown>[];
  delay_seconds: number;
  require_client_consent: boolean;
  is_enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export type ApiImportStatus = "pending" | "running" | "paused" | "succeeded" | "failed";

export interface ApiImportJob {
  id: string;
  provider: string;
  status: ApiImportStatus;
  dry_run: boolean;
  entity_type: string | null;
  cursor: Record<string, unknown>;
  user_mapping: Record<string, string>;
  counts: Record<string, number>;
  report_object_key: string | null;
  started_at: string | null;
  completed_at: string | null;
  last_error: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiImportReport {
  url: string;
  expires_in: number;
}

export interface ApiContactConsent {
  id: string;
  contact_id: string;
  channel: "email" | "telegram" | "max";
  address: string;
  normalized_address: string;
  purpose: string;
  status: "granted" | "revoked";
  source: string;
  evidence: Record<string, unknown>;
  granted_at: string | null;
  revoked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiAmoConnection {
  id: string;
  status: "connected" | "disconnected";
  account_domain: string;
  account_id: string | null;
  client_id: string;
  redirect_uri: string;
  token_expires_at: string | null;
  connected_at: string | null;
  disconnected_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiAmoOAuthStart {
  authorization_url: string;
  expires_at: string;
}

export interface ApiBackgroundJob {
  id: string;
  job_type: string;
  status: "queued" | "running" | "succeeded" | "failed";
  run_at: string;
  attempts: number;
  max_attempts: number;
  dedupe_key: string | null;
  lease_until: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiInAppNotification {
  id: string;
  subject: string | null;
  body: string;
  delivered_at: string;
  created_at: string;
}
