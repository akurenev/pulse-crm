import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bot,
  Braces,
  Check,
  ChevronRight,
  CirclePlay,
  Code2,
  Copy,
  Database,
  FileText,
  Globe2,
  Mail,
  MessageCircle,
  Pause,
  Play,
  Plus,
  RefreshCcw,
  RotateCcw,
  Settings2,
  ShieldCheck,
  Upload,
  UserPlus,
  Users,
  Webhook,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import { Button } from "../components/Button";
import { contacts as demoContacts } from "../data/demo";
import { ApiError, api, remoteEnabled } from "../lib/api";
import { parseAmoUserMapping, parseSelectOptions } from "../lib/settings-validation";
import { useAuth } from "../state/auth-store";
import { useCrm } from "../state/crm-store";
import type {
  ApiBackgroundJob,
  ApiAmoConnection,
  ApiAmoOAuthStart,
  ApiChannelConnection,
  ApiChannelKind,
  ApiContact,
  ApiContactConsent,
  ApiCustomField,
  ApiCustomFieldType,
  ApiHtmlForm,
  ApiImportJob,
  ApiImportReport,
  ApiNotificationChannel,
  ApiNotificationRule,
  ApiNotificationTemplate,
  ApiPipeline,
  ApiRequiredField,
  ApiStage,
  ApiUser,
  ApiWebhookEndpoint,
  ApiWebhookEndpointCreated,
  CursorPage,
  InvitationCreated,
} from "../types/api";
import type { Pipeline } from "../types/crm";

const DEMO_NOW = "2026-08-28T08:00:00+05:00";

const demoChannels: ApiChannelConnection[] = [
  { id: "demo-email", kind: "email", name: "Общая почта", status: "active", settings: { address: "sales@company.ru" }, default_pipeline_id: "pipeline-repeat-sales", default_stage_id: "new", default_assignee_id: null, has_credentials: true, last_healthcheck_at: DEMO_NOW, last_error: null, version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW },
  { id: "demo-telegram", kind: "telegram", name: "Telegram", status: "active", settings: { username: "@pulse_sales_bot" }, default_pipeline_id: "pipeline-repeat-sales", default_stage_id: "new", default_assignee_id: null, has_credentials: true, last_healthcheck_at: DEMO_NOW, last_error: null, version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW },
  { id: "demo-max", kind: "max", name: "MAX", status: "degraded", settings: { bot_name: "Pulse Sales" }, default_pipeline_id: "pipeline-repeat-sales", default_stage_id: "new", default_assignee_id: null, has_credentials: false, last_healthcheck_at: null, last_error: "Добавьте токен бота", version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW },
];

const demoWebhooks: ApiWebhookEndpoint[] = [
  { id: "demo-webhook", slug: "website-orders", name: "Заказы с сайта", pipeline_id: "pipeline-repeat-sales", stage_id: "new", assignee_id: null, source_id: null, is_active: true, version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW },
];

const demoForms: ApiHtmlForm[] = [
  { id: "demo-form", slug: "request-offer", title: "Запрос предложения", pipeline_id: "pipeline-repeat-sales", stage_id: "new", assignee_id: null, source_id: null, fields_schema: [], allowed_origins: ["https://company.ru"], honeypot_field: "company_website", success_message: "Спасибо! Мы свяжемся с вами.", is_active: true, version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW },
];

const demoDealFields: ApiCustomField[] = [
  { id: "demo-field-segment", entity_type: "deal", key: "customer_segment", name: "Сегмент клиента", field_type: "select", options: ["Розница", "Опт", "VIP"], is_active: true },
  { id: "demo-field-contract", entity_type: "deal", key: "contract_signed", name: "Договор подписан", field_type: "boolean", options: [], is_active: true },
];

const builtInRequiredFields = [
  ["title", "Название"],
  ["amount", "Сумма"],
  ["assignee_id", "Ответственный"],
  ["source_id", "Источник"],
  ["company_id", "Компания"],
  ["contact_ids", "Контакт"],
  ["next_purchase_at", "Следующая покупка"],
] as const;

const demoRules: ApiNotificationRule[] = [
  demoRule("Новый лид → ответственному в приложении", "lead.created", "in_app", true),
  demoRule("Входящее сообщение → ответственному в Telegram", "message.inbound.received", "telegram", true),
  demoRule("Задача просрочена → ответственному по email", "task.overdue", "email", true),
  demoRule("Следующая покупка через 7 дней → клиенту по email", "purchase.due_soon", "email", false, "client"),
];

function demoRule(name: string, eventType: string, channel: ApiNotificationChannel, enabled: boolean, audience: "employee" | "client" = "employee"): ApiNotificationRule {
  return { id: `demo-rule-${eventType}-${channel}`, template_id: `demo-template-${channel}`, name, event_type: eventType, audience, channel, pipeline_id: null, stage_id: null, source_id: null, filters: {}, recipients: [], delay_seconds: 0, require_client_consent: audience === "client", is_enabled: enabled, version: 1, created_at: DEMO_NOW, updated_at: DEMO_NOW };
}

export default function SettingsPage() {
  const { pipeline, deals, loading } = useCrm();
  return (
    <div className="page settings-page">
      <header className="page-header"><div><h1>Настройки</h1><p>Воронки, поля, каналы, уведомления и импорт</p></div></header>
      <Tabs.Root defaultValue="pipelines" orientation="vertical" className="settings-layout">
        <Tabs.List className="settings-nav" aria-label="Разделы настроек">
          <Tabs.Trigger value="pipelines"><Settings2 size={18} /> Воронки и поля</Tabs.Trigger>
          <Tabs.Trigger value="users"><Users size={18} /> Пользователи</Tabs.Trigger>
          <Tabs.Trigger value="channels"><MessageCircle size={18} /> Каналы</Tabs.Trigger>
          <Tabs.Trigger value="notifications"><ShieldCheck size={18} /> Оповещения</Tabs.Trigger>
          <Tabs.Trigger value="import"><Database size={18} /> Импорт amoCRM</Tabs.Trigger>
        </Tabs.List>
        <div className="settings-content">
          <Tabs.Content value="pipelines"><PipelinesPanel currentPipeline={pipeline} dealsCount={deals.length} /></Tabs.Content>
          <Tabs.Content value="users"><InviteUsersPanel /></Tabs.Content>
          <Tabs.Content value="channels"><ChannelsPanel pipeline={pipeline} routingLoading={loading} /></Tabs.Content>
          <Tabs.Content value="notifications"><NotificationsPanel pipeline={pipeline} /></Tabs.Content>
          <Tabs.Content value="import"><ImportPanel /></Tabs.Content>
        </div>
      </Tabs.Root>
    </div>
  );
}

function PipelinesPanel({ currentPipeline, dealsCount }: { currentPipeline: Pipeline; dealsCount: number }) {
  const [editor, setEditor] = useState<"pipeline" | "custom-field" | null>(null);
  const [selectedStage, setSelectedStage] = useState<{ pipelineName: string; stage: ApiStage } | null>(null);
  const [localPipelines, setLocalPipelines] = useState<ApiPipeline[]>([pipelineToApi(currentPipeline)]);
  const [localCustomFields, setLocalCustomFields] = useState<ApiCustomField[]>(demoDealFields);
  const [requiredByStage, setRequiredByStage] = useState<Record<string, ApiRequiredField[]>>({});
  const pipelinesQuery = useQuery({ queryKey: ["settings", "pipelines"], queryFn: () => api.get<ApiPipeline[]>("/pipelines"), enabled: remoteEnabled });
  const customFieldsQuery = useQuery({ queryKey: ["settings", "custom-fields", "deal"], queryFn: () => api.get<ApiCustomField[]>("/custom-fields?entity_type=deal"), enabled: remoteEnabled });
  const pipelines = remoteEnabled ? pipelinesQuery.data ?? [] : localPipelines;
  const customFields = remoteEnabled ? customFieldsQuery.data ?? [] : localCustomFields;

  async function created(createdPipeline: ApiPipeline) {
    if (remoteEnabled) await pipelinesQuery.refetch();
    else setLocalPipelines((items) => [...items, createdPipeline]);
    setEditor(null);
  }

  async function customFieldCreated(createdField: ApiCustomField) {
    if (remoteEnabled) await customFieldsQuery.refetch();
    else setLocalCustomFields((items) => [...items, createdField]);
    setEditor(null);
  }

  function requiredFieldsSaved(stageId: string, fields: ApiRequiredField[]) {
    setRequiredByStage((current) => ({ ...current, [stageId]: fields }));
    setSelectedStage(null);
  }

  return (
    <>
      <SettingsHeading title="Воронки и обязательные поля" action="Новая воронка" onAction={() => { setSelectedStage(null); setEditor((value) => value === "pipeline" ? null : "pipeline"); }} />
      <div className="settings-quick-actions" aria-label="Настройка полей сделки">
        <Button compact onClick={() => { setSelectedStage(null); setEditor((value) => value === "custom-field" ? null : "custom-field"); }}><Plus size={15} /> Новое поле сделки</Button>
      </div>
      {editor === "pipeline" ? <PipelineEditor position={pipelines.length} onCancel={() => setEditor(null)} onCreated={created} /> : null}
      {editor === "custom-field" ? <CustomFieldEditor onCancel={() => setEditor(null)} onCreated={customFieldCreated} /> : null}
      {selectedStage ? <RequiredFieldsEditor pipelineName={selectedStage.pipelineName} stage={selectedStage.stage} customFields={customFields} demoFields={requiredByStage[selectedStage.stage.id] ?? []} onCancel={() => setSelectedStage(null)} onSaved={requiredFieldsSaved} /> : null}
      {pipelinesQuery.isError ? <SettingsNotice tone="error">Не удалось загрузить список воронок.</SettingsNotice> : null}
      {customFieldsQuery.isError ? <SettingsNotice tone="error">Не удалось загрузить пользовательские поля сделок.</SettingsNotice> : null}
      <section className="settings-field-catalog" aria-label="Поля сделок">
        <header><div><strong>Пользовательские поля сделок</strong><span>Доступны в карточке и в правилах обязательности этапов.</span></div><em>{customFields.length}</em></header>
        <div>
          {customFields.map((field) => <span className="settings-field-chip" key={field.id}><strong>{field.name}</strong><small>{fieldTypeLabel(field.field_type)} · {field.key}</small></span>)}
          {!customFields.length && !customFieldsQuery.isLoading ? <small>Пока используются только встроенные поля.</small> : null}
          {customFieldsQuery.isLoading ? <small>Загружаем поля…</small> : null}
        </div>
      </section>
      {pipelines.map((pipeline) => (
        <section className="settings-section" key={pipeline.id}>
          <header><div><strong>{pipeline.name}</strong><span>{pipeline.stages.length} этапов · {pipeline.id === currentPipeline.id ? `${dealsCount} активных сделок` : "активная воронка"}</span></div><ChevronRight size={20} /></header>
          <div className="stage-settings">
            {pipeline.stages.map((stage, index) => {
              const savedFields = requiredByStage[stage.id];
              const summary = savedFields ? savedFields.length ? `${savedFields.length} обязательных полей` : "Обязательные поля не выбраны" : "Настроить обязательные поля";
              return <button type="button" key={stage.id} aria-label={`Обязательные поля этапа ${stage.name}`} disabled={remoteEnabled && (customFieldsQuery.isLoading || customFieldsQuery.isError)} onClick={() => { setEditor(null); setSelectedStage({ pipelineName: pipeline.name, stage }); }}><i className="stage-dot" style={{ backgroundColor: stage.color }} /><span>{index + 1}</span><strong>{stage.name}</strong><small>{customFieldsQuery.isLoading && remoteEnabled ? "Загружаем поля…" : summary}</small><ChevronRight size={16} aria-hidden="true" /></button>;
            })}
          </div>
        </section>
      ))}
    </>
  );
}

function PipelineEditor({ position, onCancel, onCreated }: { position: number; onCancel: () => void; onCreated: (pipeline: ApiPipeline) => void | Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving(true);
    setError("");
    const payload = {
      name: String(data.get("name")).trim(),
      position,
      stages: [
        { name: "Новый лид", color: "#4B96F8", position: 0, stage_type: "open" },
        { name: "Связались", color: "#6D5DF7", position: 1, stage_type: "open" },
        { name: "Предложение", color: "#20B878", position: 2, stage_type: "open" },
        { name: "Успешно реализовано", color: "#16A36D", position: 3, stage_type: "won" },
        { name: "Закрыто и не реализовано", color: "#929AAA", position: 4, stage_type: "lost" },
      ],
    };
    try {
      const created = remoteEnabled ? await api.post<ApiPipeline>("/pipelines", payload) : demoPipeline(payload.name, position);
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось создать воронку. Проверьте уникальность названия."));
    } finally {
      setSaving(false);
    }
  }
  return (
    <SettingsEditor title="Новая воронка" icon={<Settings2 size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid"><label className="field"><span>Название воронки</span><input name="name" required maxLength={160} placeholder="Например, Корпоративные продажи" /></label></div>
        <p className="settings-form-help">Будут созданы три рабочих этапа, а также системные этапы успешного и неуспешного закрытия.</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Создать воронку" />
      </form>
    </SettingsEditor>
  );
}

function CustomFieldEditor({ onCancel, onCreated }: { onCancel: () => void; onCreated: (field: ApiCustomField) => void | Promise<void> }) {
  const [fieldType, setFieldType] = useState<ApiCustomFieldType>("text");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving(true);
    setError("");
    try {
      const options = fieldType === "select" ? parseSelectOptions(String(data.get("options") ?? "")) : [];
      if (fieldType === "select" && !options.length) throw new Error("Добавьте хотя бы один вариант для поля-списка.");
      const payload = {
        entity_type: "deal" as const,
        name: String(data.get("name")).trim(),
        key: String(data.get("key")).trim().toLowerCase(),
        field_type: fieldType,
        options,
      };
      const created = remoteEnabled ? await api.post<ApiCustomField>("/custom-fields", payload) : demoCustomField(payload);
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось создать поле. Проверьте системный ключ и варианты списка."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsEditor title="Новое поле сделки" icon={<FileText size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid">
          <label className="field"><span>Название</span><input name="name" required maxLength={120} placeholder="Например, Размер компании" /></label>
          <label className="field"><span>Системный ключ</span><input name="key" required maxLength={64} pattern="[a-z][a-z0-9_]*" title="Латинские строчные буквы, цифры и подчёркивание; первый символ — буква" placeholder="company_size" /></label>
          <label className="field"><span>Тип</span><select name="field_type" value={fieldType} onChange={(event) => setFieldType(event.target.value as ApiCustomFieldType)}><option value="text">Текст</option><option value="number">Число</option><option value="date">Дата</option><option value="boolean">Флаг</option><option value="select">Список</option></select></label>
        </div>
        {fieldType === "select" ? <label className="field"><span>Варианты списка</span><textarea name="options" required rows={4} placeholder={"Розница\nОпт\nVIP"} /></label> : null}
        <p className="settings-form-help">Ключ используется в API и импорте: латинские строчные буквы, цифры и подчёркивание. После создания тип и ключ не меняются.</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Создать поле" />
      </form>
    </SettingsEditor>
  );
}

function RequiredFieldsEditor({ pipelineName, stage, customFields, demoFields, onCancel, onSaved }: { pipelineName: string; stage: ApiStage; customFields: ApiCustomField[]; demoFields: ApiRequiredField[]; onCancel: () => void; onSaved: (stageId: string, fields: ApiRequiredField[]) => void }) {
  const requiredQuery = useQuery({
    queryKey: ["settings", "required-fields", stage.id],
    queryFn: () => api.get<ApiRequiredField[]>(`/stages/${stage.id}/required-fields`),
    enabled: remoteEnabled,
    retry: false,
  });

  if (remoteEnabled && requiredQuery.isLoading) {
    return <SettingsEditor title={`Обязательные поля · ${stage.name}`} icon={<Check size={19} />} onCancel={onCancel}><div className="settings-editor__body"><SettingsNotice tone="neutral">Загружаем настройки этапа…</SettingsNotice></div></SettingsEditor>;
  }
  if (remoteEnabled && requiredQuery.isError) {
    return <SettingsEditor title={`Обязательные поля · ${stage.name}`} icon={<Check size={19} />} onCancel={onCancel}><div className="settings-editor__body"><SettingsNotice tone="error">Не удалось загрузить обязательные поля этапа.</SettingsNotice><Button compact onClick={() => void requiredQuery.refetch()}><RefreshCcw size={15} /> Повторить</Button></div></SettingsEditor>;
  }

  const initialFields = remoteEnabled ? requiredQuery.data ?? [] : demoFields;
  const stateKey = `${stage.id}:${initialFields.map((field) => field.built_in_key ?? field.field_definition_id).join(",")}`;
  return <RequiredFieldsForm key={stateKey} pipelineName={pipelineName} stage={stage} customFields={customFields} initialFields={initialFields} onCancel={onCancel} onSaved={onSaved} />;
}

function RequiredFieldsForm({ pipelineName, stage, customFields, initialFields, onCancel, onSaved }: { pipelineName: string; stage: ApiStage; customFields: ApiCustomField[]; initialFields: ApiRequiredField[]; onCancel: () => void; onSaved: (stageId: string, fields: ApiRequiredField[]) => void }) {
  const [selected, setSelected] = useState(() => new Set(initialFields.map((field) => field.built_in_key ? `built-in:${field.built_in_key}` : `custom:${field.field_definition_id}`)));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  function toggle(value: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(value)) next.delete(value); else next.add(value);
      return next;
    });
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    const fields = [
      ...builtInRequiredFields.filter(([key]) => selected.has(`built-in:${key}`)).map(([key]) => ({ built_in_key: key })),
      ...customFields.filter((field) => selected.has(`custom:${field.id}`)).map((field) => ({ field_definition_id: field.id })),
    ];
    try {
      const saved = remoteEnabled ? await api.put<ApiRequiredField[]>(`/stages/${stage.id}/required-fields`, { fields }) : demoRequiredFields(fields);
      onSaved(stage.id, saved);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось сохранить обязательные поля этапа."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsEditor title={`Обязательные поля · ${stage.name}`} icon={<Check size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <p className="settings-form-help">Воронка «{pipelineName}». Лид сохранится на текущем этапе, но переход дальше будет заблокирован, пока выбранные поля не заполнены.</p>
        <fieldset className="required-fields-group">
          <legend>Встроенные поля</legend>
          <div className="required-fields-grid">
            {builtInRequiredFields.map(([key, label]) => <label key={key}><input type="checkbox" checked={selected.has(`built-in:${key}`)} onChange={() => toggle(`built-in:${key}`)} /><span><strong>{label}</strong><small>{key}</small></span></label>)}
          </div>
        </fieldset>
        <fieldset className="required-fields-group">
          <legend>Пользовательские поля сделки</legend>
          {customFields.length ? <div className="required-fields-grid">{customFields.map((field) => <label key={field.id}><input type="checkbox" checked={selected.has(`custom:${field.id}`)} onChange={() => toggle(`custom:${field.id}`)} /><span><strong>{field.name}</strong><small>{fieldTypeLabel(field.field_type)} · {field.key}</small></span></label>)}</div> : <p className="settings-form-help">Пользовательских полей пока нет. Создайте поле сделки, затем вернитесь к этапу.</p>}
        </fieldset>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Сохранить обязательность" />
      </form>
    </SettingsEditor>
  );
}

function ChannelsPanel({ pipeline, routingLoading }: { pipeline: Pipeline; routingLoading: boolean }) {
  const [editor, setEditor] = useState<"channel" | "webhook" | "form" | null>(null);
  const [localChannels, setLocalChannels] = useState(demoChannels);
  const [localWebhooks, setLocalWebhooks] = useState(demoWebhooks);
  const [localForms, setLocalForms] = useState(demoForms);
  const [webhookSecret, setWebhookSecret] = useState<ApiWebhookEndpointCreated | null>(null);
  const channelsQuery = useQuery({ queryKey: ["settings", "channels"], queryFn: () => api.get<ApiChannelConnection[]>("/admin/integrations/channels"), enabled: remoteEnabled });
  const formsQuery = useQuery({ queryKey: ["settings", "forms"], queryFn: () => api.get<ApiHtmlForm[]>("/admin/integrations/forms"), enabled: remoteEnabled });
  const webhooksQuery = useQuery({ queryKey: ["settings", "webhooks"], queryFn: () => api.get<ApiWebhookEndpoint[]>("/admin/integrations/webhooks"), enabled: remoteEnabled });
  const channels = remoteEnabled ? channelsQuery.data ?? [] : localChannels;
  const forms = remoteEnabled ? formsQuery.data ?? [] : localForms;
  const webhooks = remoteEnabled ? webhooksQuery.data ?? [] : localWebhooks;
  const loadFailed = channelsQuery.isError || formsQuery.isError || webhooksQuery.isError;

  async function createdChannel(created: ApiChannelConnection) {
    if (remoteEnabled) await channelsQuery.refetch(); else setLocalChannels((items) => [created, ...items]);
    setEditor(null);
  }
  async function createdWebhook(created: ApiWebhookEndpointCreated) {
    if (remoteEnabled) await webhooksQuery.refetch(); else setLocalWebhooks((items) => [created, ...items]);
    setWebhookSecret(created);
    setEditor(null);
  }
  async function createdForm(created: ApiHtmlForm) {
    if (remoteEnabled) await formsQuery.refetch(); else setLocalForms((items) => [created, ...items]);
    setEditor(null);
  }

  return (
    <>
      <SettingsHeading title="Общие каналы компании" action="Подключить канал" onAction={() => setEditor((current) => current === "channel" ? null : "channel")} />
      <div className="settings-quick-actions" aria-label="Добавить источник">
        <Button compact onClick={() => setEditor((current) => current === "webhook" ? null : "webhook")}><Webhook size={16} /> Webhook</Button>
        <Button compact onClick={() => setEditor((current) => current === "form" ? null : "form")}><Code2 size={16} /> HTML-форма</Button>
      </div>
      {routingLoading && remoteEnabled ? <SettingsNotice tone="neutral">Загружаем маршруты воронки…</SettingsNotice> : null}
      {loadFailed ? <SettingsNotice tone="error">Не удалось загрузить часть подключений. Обновите страницу или проверьте доступ администратора.</SettingsNotice> : null}
      {editor === "channel" ? <ChannelEditor pipeline={pipeline} onCancel={() => setEditor(null)} onCreated={createdChannel} /> : null}
      {editor === "webhook" ? <WebhookEditor pipeline={pipeline} onCancel={() => setEditor(null)} onCreated={createdWebhook} /> : null}
      {editor === "form" ? <HtmlFormEditor pipeline={pipeline} onCancel={() => setEditor(null)} onCreated={createdForm} /> : null}
      {webhookSecret ? <WebhookSecret result={webhookSecret} onClose={() => setWebhookSecret(null)} /> : null}
      <section className="channel-list" aria-label="Подключённые источники">
        {channels.map((channel) => <div className="channel-row" key={channel.id}><span className="channel-icon">{channel.kind === "email" ? <Mail size={20} /> : <Bot size={20} />}</span><span><strong>{channel.name}</strong><small>{channelLabel(channel.kind)} · {routeLabel(channel.default_stage_id, pipeline)}</small>{channel.last_error ? <small className="settings-inline-error">{channel.last_error}</small> : null}</span><StatusPill status={channel.status} /><ChevronRight size={18} aria-hidden="true" /></div>)}
        {webhooks.map((endpoint) => <div className="channel-row" key={endpoint.id}><span className="channel-icon"><Braces size={20} /></span><span><strong>{endpoint.name}</strong><small>POST /hooks/v1/generic/{endpoint.slug}</small></span><StatusPill status={endpoint.is_active ? "active" : "disabled"} /><ChevronRight size={18} aria-hidden="true" /></div>)}
        {forms.map((form) => <div className="channel-row" key={form.id}><span className="channel-icon"><FileText size={20} /></span><span><strong>{form.title}</strong><small>/forms/{form.slug} · {form.allowed_origins.length || "только Pulse"} домен(а)</small></span><StatusPill status={form.is_active ? "active" : "disabled"} /><ChevronRight size={18} aria-hidden="true" /></div>)}
        {!channels.length && !webhooks.length && !forms.length && !channelsQuery.isLoading ? <SettingsEmpty icon={<MessageCircle size={22} />} title="Источники пока не подключены" text="Добавьте корпоративную почту, бота, webhook или форму." /> : null}
      </section>
    </>
  );
}

function ChannelEditor({ pipeline, onCancel, onCreated }: { pipeline: Pipeline; onCancel: () => void; onCreated: (created: ApiChannelConnection) => void | Promise<void> }) {
  const [kind, setKind] = useState<ApiChannelKind>("email");
  const [credentials, setCredentials] = useState(credentialsExample("email"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const stageId = String(data.get("stage_id"));
    setSaving(true);
    setError("");
    try {
      const parsed = JSON.parse(credentials) as unknown;
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("JSON должен быть объектом");
      const payload = { kind, name: String(data.get("name")).trim(), status: "active", credentials: parsed, settings: {}, default_pipeline_id: pipeline.id, default_stage_id: stageId, default_assignee_id: null };
      const created = remoteEnabled ? await api.post<ApiChannelConnection>("/admin/integrations/channels", payload) : demoChannel(payload.kind, payload.name, pipeline.id, stageId);
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось подключить канал. Проверьте JSON и реквизиты."));
    } finally {
      setSaving(false);
    }
  }
  return (
    <SettingsEditor title="Подключение корпоративного канала" icon={<Bot size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid">
          <label className="field"><span>Тип канала</span><select name="kind" value={kind} onChange={(event) => { const next = event.target.value as ApiChannelKind; setKind(next); setCredentials(credentialsExample(next)); }}><option value="email">Email (IMAP/SMTP)</option><option value="telegram">Telegram-бот</option><option value="max">MAX-бот</option></select></label>
          <label className="field"><span>Название</span><input name="name" required maxLength={160} placeholder="Например, Общая почта" /></label>
          <label className="field"><span>Воронка</span><input value={pipeline.name} readOnly /></label>
          <label className="field"><span>Начальный этап</span><select name="stage_id" required>{pipeline.stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></label>
        </div>
        <label className="field settings-json"><span>Реквизиты (сохраняются зашифрованными)</span><textarea value={credentials} onChange={(event) => setCredentials(event.target.value)} rows={kind === "email" ? 12 : 6} spellCheck={false} /></label>
        <p className="settings-form-help">Для ботов webhook_secret должен совпадать с секретом, заданным у провайдера. Секреты после сохранения больше не отображаются.</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Подключить" />
      </form>
    </SettingsEditor>
  );
}

function WebhookEditor({ pipeline, onCancel, onCreated }: { pipeline: Pipeline; onCancel: () => void; onCreated: (created: ApiWebhookEndpointCreated) => void | Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const defaultSlug = useMemo(() => `incoming-${Date.now().toString(36)}`, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving(true);
    setError("");
    try {
      const payload = { name: String(data.get("name")).trim(), slug: String(data.get("slug")).trim().toLocaleLowerCase(), pipeline_id: pipeline.id, stage_id: String(data.get("stage_id")), assignee_id: null, source_id: null, is_active: true };
      const created = remoteEnabled ? await api.post<ApiWebhookEndpointCreated>("/admin/integrations/webhooks", payload) : { ...demoWebhook(payload.name, payload.slug, payload.pipeline_id, payload.stage_id), secret: `demo_${crypto.randomUUID().replaceAll("-", "")}` };
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось создать webhook. Проверьте уникальность slug."));
    } finally {
      setSaving(false);
    }
  }
  return (
    <SettingsEditor title="Универсальный webhook" icon={<Webhook size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid">
          <label className="field"><span>Название</span><input name="name" required maxLength={160} placeholder="Заказы с сайта" /></label>
          <label className="field"><span>Slug endpoint</span><input name="slug" required minLength={12} maxLength={100} pattern="[a-z0-9]+(?:-[a-z0-9]+)*" defaultValue={defaultSlug} /></label>
          <label className="field"><span>Воронка</span><input value={pipeline.name} readOnly /></label>
          <label className="field"><span>Начальный этап</span><select name="stage_id" required>{pipeline.stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></label>
        </div>
        <p className="settings-form-help">Pulse создаст HMAC-SHA256 секрет и покажет его один раз. Входящие запросы должны передавать timestamp и Idempotency-Key.</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Создать endpoint" />
      </form>
    </SettingsEditor>
  );
}

function HtmlFormEditor({ pipeline, onCancel, onCreated }: { pipeline: Pipeline; onCancel: () => void; onCreated: (created: ApiHtmlForm) => void | Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const origins = String(data.get("allowed_origins")).split(/[\s,]+/).map((value) => value.trim()).filter(Boolean);
    setSaving(true);
    setError("");
    try {
      const payload = { title: String(data.get("title")).trim(), slug: String(data.get("slug")).trim().toLocaleLowerCase(), pipeline_id: pipeline.id, stage_id: String(data.get("stage_id")), assignee_id: null, source_id: null, fields_schema: [{ key: "name", type: "text", label: "Имя", required: true, max_length: 200 }, { key: "email", type: "email", label: "Email", required: false, max_length: 320 }, { key: "phone", type: "phone", label: "Телефон", required: false, max_length: 80 }, { key: "message", type: "textarea", label: "Сообщение", required: false, max_length: 2_000 }], allowed_origins: origins, honeypot_field: "company_website", success_message: "Спасибо! Мы свяжемся с вами.", is_active: true };
      const created = remoteEnabled ? await api.post<ApiHtmlForm>("/admin/integrations/forms", payload) : demoForm(payload.title, payload.slug, payload.pipeline_id, payload.stage_id, origins);
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось создать HTML-форму. Проверьте slug и домены."));
    } finally {
      setSaving(false);
    }
  }
  return (
    <SettingsEditor title="HTML-форма для сайта" icon={<Globe2 size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid">
          <label className="field"><span>Название формы</span><input name="title" required maxLength={240} placeholder="Запрос предложения" /></label>
          <label className="field"><span>Slug</span><input name="slug" required minLength={4} maxLength={100} pattern="[a-z0-9]+(?:-[a-z0-9]+)*" placeholder="request-offer" /></label>
          <label className="field"><span>Воронка</span><input value={pipeline.name} readOnly /></label>
          <label className="field"><span>Начальный этап</span><select name="stage_id" required>{pipeline.stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}</select></label>
        </div>
        <label className="field"><span>Разрешённые origin, через запятую</span><input name="allowed_origins" placeholder="https://company.ru, https://promo.company.ru" /></label>
        <p className="settings-form-help">Будут созданы поля «Имя», email, телефон и сообщение, а также honeypot-защита. Готовая форма откроется по /forms/slug.</p>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Создать форму" />
      </form>
    </SettingsEditor>
  );
}

function WebhookSecret({ result, onClose }: { result: ApiWebhookEndpointCreated; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  async function copySecret() { try { await navigator.clipboard.writeText(result.secret); setCopied(true); } catch { setCopied(false); } }
  return <section className="settings-secret" role="status"><span><ShieldCheck size={20} /></span><div><strong>Сохраните секрет webhook сейчас</strong><small>После закрытия Pulse CRM больше не покажет это значение.</small><code>{result.secret}</code><p>Endpoint: <code>/hooks/v1/generic/{result.slug}</code></p></div><Button compact onClick={() => void copySecret()}><Copy size={15} /> {copied ? "Скопировано" : "Копировать"}</Button><button className="icon-button" type="button" aria-label="Закрыть" onClick={onClose}><X size={17} /></button></section>;
}

function NotificationsPanel({ pipeline }: { pipeline: Pipeline }) {
  const { session } = useAuth();
  const [showEditor, setShowEditor] = useState(false);
  const [localRules, setLocalRules] = useState(demoRules);
  const [savingRuleId, setSavingRuleId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const rulesQuery = useQuery({ queryKey: ["settings", "notification-rules"], queryFn: () => api.get<ApiNotificationRule[]>("/admin/integrations/notification-rules"), enabled: remoteEnabled });
  const templatesQuery = useQuery({ queryKey: ["settings", "notification-templates"], queryFn: () => api.get<ApiNotificationTemplate[]>("/admin/integrations/notification-templates"), enabled: remoteEnabled });
  const rules = remoteEnabled ? rulesQuery.data ?? [] : localRules;

  async function createdRule(created: ApiNotificationRule) {
    if (remoteEnabled) await Promise.all([rulesQuery.refetch(), templatesQuery.refetch()]); else setLocalRules((items) => [created, ...items]);
    setShowEditor(false);
  }
  async function toggleRule(rule: ApiNotificationRule) {
    setSavingRuleId(rule.id);
    setError("");
    try {
      if (remoteEnabled) { await api.patch<ApiNotificationRule>(`/admin/integrations/notification-rules/${rule.id}`, { expected_version: rule.version, is_enabled: !rule.is_enabled }); await rulesQuery.refetch(); }
      else setLocalRules((items) => items.map((item) => item.id === rule.id ? { ...item, is_enabled: !item.is_enabled, version: item.version + 1 } : item));
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось изменить правило."));
    } finally {
      setSavingRuleId(null);
    }
  }
  return (
    <>
      <SettingsHeading title="Правила оповещений" action="Новое правило" onAction={() => setShowEditor((value) => !value)} />
      {rulesQuery.isError || templatesQuery.isError ? <SettingsNotice tone="error">Не удалось загрузить правила оповещений.</SettingsNotice> : null}
      {showEditor ? <NotificationEditor pipeline={pipeline} currentUserId={session?.user.id ?? "demo-user"} onCancel={() => setShowEditor(false)} onCreated={createdRule} /> : null}
      {error ? <SettingsNotice tone="error">{error}</SettingsNotice> : null}
      <section className="rules-list" aria-label="Правила оповещений">
        {rules.map((rule) => <div key={rule.id}><button type="button" className={`rule-toggle${rule.is_enabled ? " is-on" : ""}`} aria-label={`${rule.is_enabled ? "Выключить" : "Включить"} правило ${rule.name}`} aria-pressed={rule.is_enabled} disabled={savingRuleId === rule.id} onClick={() => void toggleRule(rule)}><i /></button><strong>{rule.name}</strong><small>{rule.is_enabled ? "Включено" : "Выключено"} · {notificationChannelLabel(rule.channel)}</small><ChevronRight size={18} aria-hidden="true" /></div>)}
        {!rules.length && !rulesQuery.isLoading ? <SettingsEmpty icon={<ShieldCheck size={22} />} title="Правил пока нет" text="Создайте шаблон и правило из каталога событий." /> : null}
      </section>
    </>
  );
}

function NotificationEditor({ pipeline, currentUserId, onCancel, onCreated }: { pipeline: Pipeline; currentUserId: string; onCancel: () => void; onCreated: (created: ApiNotificationRule) => void | Promise<void> }) {
  const [audience, setAudience] = useState<"employee" | "client">("employee");
  const [channel, setChannel] = useState<ApiNotificationChannel>("in_app");
  const [recipientAddress, setRecipientAddress] = useState("");
  const [contactId, setContactId] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const contactsQuery = useQuery({ queryKey: ["settings", "notification-contacts"], queryFn: () => api.get<CursorPage<ApiContact>>("/contacts?limit=100"), enabled: remoteEnabled && audience === "client" });
  const clientContacts = remoteEnabled
    ? (contactsQuery.data?.items ?? []).map((contact) => ({ id: contact.id, name: `${contact.first_name} ${contact.last_name}`.trim(), email: contact.primary_email ?? "" }))
    : demoContacts.map((contact) => ({ id: contact.id, name: contact.name, email: contact.email === "—" ? "" : contact.email }));

  function changeAudience(value: "employee" | "client") {
    setAudience(value);
    setEnabled(value === "employee");
    if (value === "client" && channel === "in_app") setChannel("email");
  }

  function changeContact(value: string) {
    setContactId(value);
    const contact = clientContacts.find((item) => item.id === value);
    if (channel === "email" && contact?.email) setRecipientAddress(contact.email);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name")).trim();
    setSaving(true);
    setError("");
    try {
      const address = channel === "in_app" ? currentUserId : recipientAddress.trim();
      let recipient: Record<string, string> = channel === "in_app"
        ? { address, recipient_id: currentUserId }
        : { address };
      if (audience === "client") {
        if (!contactId || !address || data.get("consent_confirmed") !== "on") {
          throw new Error("Выберите клиента, адрес и подтвердите основание согласия.");
        }
        const evidenceText = String(data.get("consent_evidence") ?? "").trim();
        if (!evidenceText) throw new Error("Опишите, где и когда клиент дал согласие.");
        const normalizedAddress = remoteEnabled
          ? (await api.post<ApiContactConsent>(`/contacts/${contactId}/consents`, { channel, address, purpose: "notifications", source: "manual", evidence: { confirmation: evidenceText, captured_by: currentUserId } })).normalized_address
          : channel === "email" ? address.toLocaleLowerCase() : address;
        recipient = { address, contact_id: contactId, normalized_address: normalizedAddress };
      }
      const body = String(data.get("body")).trim();
      const template = remoteEnabled ? await api.post<ApiNotificationTemplate>("/admin/integrations/notification-templates", { name: `${name} — шаблон`, channel, subject_template: channel === "email" ? "Событие в Pulse CRM" : null, body_template: body, is_active: true }) : demoTemplate(`${name} — шаблон`, channel, body);
      const payload = { template_id: template.id, name, event_type: String(data.get("event_type")), audience, channel, pipeline_id: data.get("pipeline_filter") === "on" ? pipeline.id : null, stage_id: null, source_id: null, filters: {}, recipients: [recipient], delay_seconds: Math.max(0, Number(data.get("delay_minutes")) || 0) * 60, require_client_consent: true, is_enabled: audience === "client" ? false : enabled };
      const created = remoteEnabled ? await api.post<ApiNotificationRule>("/admin/integrations/notification-rules", payload) : { ...demoRule(payload.name, payload.event_type, channel, payload.is_enabled, audience), template_id: template.id, pipeline_id: payload.pipeline_id, recipients: payload.recipients, delay_seconds: payload.delay_seconds };
      await onCreated(created);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось создать шаблон и правило."));
    } finally {
      setSaving(false);
    }
  }
  return (
    <SettingsEditor title="Новое правило и шаблон" icon={<ShieldCheck size={19} />} onCancel={onCancel}>
      <form onSubmit={(event) => void submit(event)}>
        <div className="settings-form-grid">
          <label className="field"><span>Название правила</span><input name="name" required maxLength={160} placeholder="Новый лид — владельцу" /></label>
          <label className="field"><span>Событие</span><select name="event_type" defaultValue="lead.created">{notificationEvents.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label className="field"><span>Получатель</span><select name="audience" value={audience} onChange={(event) => changeAudience(event.target.value as "employee" | "client")}><option value="employee">Сотрудник</option><option value="client">Клиент с согласием</option></select></label>
          <label className="field"><span>Канал</span><select name="channel" value={channel} onChange={(event) => { const next = event.target.value as ApiNotificationChannel; setChannel(next); if (audience === "client" && next === "email") { const contact = clientContacts.find((item) => item.id === contactId); if (contact?.email) setRecipientAddress(contact.email); } }}><option value="in_app" disabled={audience === "client"}>В приложении</option><option value="email">Email</option><option value="telegram">Telegram</option><option value="max">MAX</option></select></label>
          {audience === "client" ? <label className="field"><span>Клиент</span><select aria-label="Клиент" name="contact_id" value={contactId} onChange={(event) => changeContact(event.target.value)} required><option value="">Выберите контакт</option>{clientContacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.name}</option>)}</select></label> : null}
          {channel === "in_app" ? <label className="field"><span>Адрес</span><input value="Текущий администратор" readOnly /></label> : <label className="field"><span>{channel === "email" ? "Email" : "ID чата получателя"}</span><input name="recipient" value={recipientAddress} onChange={(event) => setRecipientAddress(event.target.value)} required placeholder={channel === "email" ? "client@company.ru" : "ID чата получателя"} /></label>}
          <label className="field"><span>Задержка, минут</span><input name="delay_minutes" type="number" min="0" max="525600" defaultValue="0" /></label>
        </div>
        {audience === "client" ? <><label className="field"><span>Основание согласия</span><textarea name="consent_evidence" required rows={2} placeholder="Например: checkbox формы заказа, 28.08.2026" /></label><div className="settings-checks"><label><input name="consent_confirmed" type="checkbox" required /> Подтверждаю, что согласие клиента получено и зафиксировано</label></div></> : null}
        <label className="field"><span>Текст шаблона</span><textarea name="body" required rows={3} defaultValue="В Pulse CRM произошло новое событие. Откройте карточку, чтобы увидеть детали." /></label>
        <div className="settings-checks"><label><input name="pipeline_filter" type="checkbox" /> Только воронка «{pipeline.name}»</label><label><input name="is_enabled" type="checkbox" checked={enabled} disabled={audience === "client"} onChange={(event) => setEnabled(event.target.checked)} /> Включить сразу</label></div>
        <p className="settings-form-help">Клиентское правило всегда создаётся выключенным. После проверки адреса и сохранённого согласия включите его в списке правил.</p>
        {contactsQuery.isError ? <p className="form-error" role="alert">Не удалось загрузить контакты для согласия.</p> : null}
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <EditorActions saving={saving} onCancel={onCancel} submitLabel="Создать правило" />
      </form>
    </SettingsEditor>
  );
}

function ImportPanel() {
  const [localImports, setLocalImports] = useState<ApiImportJob[]>([]);
  const [localConnection, setLocalConnection] = useState<ApiAmoConnection | null>(null);
  const [showConnect, setShowConnect] = useState(false);
  const [showStart, setShowStart] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [authorizationUrl, setAuthorizationUrl] = useState("");
  const oauthWindow = useRef<Window | null>(null);
  const connectionQuery = useQuery<ApiAmoConnection | null>({
    queryKey: ["settings", "amocrm-connection"],
    queryFn: async () => {
      try {
        return await api.get<ApiAmoConnection>("/admin/integrations/amocrm/connection");
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 404) return null;
        throw reason;
      }
    },
    enabled: remoteEnabled,
    retry: false,
  });
  const importsQuery = useQuery({ queryKey: ["settings", "amo-imports"], queryFn: () => api.get<ApiImportJob[]>("/admin/integrations/imports"), enabled: remoteEnabled, refetchInterval: remoteEnabled ? 15_000 : false });
  const failedJobsQuery = useQuery({ queryKey: ["settings", "failed-jobs"], queryFn: () => api.get<ApiBackgroundJob[]>("/admin/jobs?status=failed"), enabled: remoteEnabled, refetchInterval: remoteEnabled ? 15_000 : false });
  const connection = remoteEnabled ? connectionQuery.data ?? null : localConnection;
  const connected = connection?.status === "connected";
  const imports = remoteEnabled ? importsQuery.data ?? [] : localImports;
  const failedJobs = failedJobsQuery.data ?? [];

  useEffect(() => {
    function receiveOAuthResult(event: MessageEvent<unknown>) {
      if (event.origin !== window.location.origin) return;
      if (oauthWindow.current !== null && event.source !== oauthWindow.current) return;
      const data = event.data;
      if (!data || typeof data !== "object" || !("type" in data) || !("status" in data)) return;
      const result = data as { type?: unknown; status?: unknown };
      if (result.type !== "pulse:amocrm-oauth") return;
      if (result.status === "ok") {
        setError("");
        setAuthorizationUrl("");
        setShowConnect(false);
        void connectionQuery.refetch();
      } else {
        setError("amoCRM не завершила подключение. Проверьте Integration ID, секрет и Redirect URI.");
      }
      oauthWindow.current = null;
    }
    window.addEventListener("message", receiveOAuthResult);
    return () => window.removeEventListener("message", receiveOAuthResult);
  }, [connectionQuery]);

  async function connectAmo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving("connect");
    setError("");
    setAuthorizationUrl("");
    const popup = remoteEnabled
      ? window.open("", "pulse-amocrm-oauth", "popup,width=720,height=800")
      : null;
    oauthWindow.current = popup;
    try {
      const accountDomain = String(data.get("account_domain")).trim();
      if (!remoteEnabled) {
        const now = new Date().toISOString();
        setLocalConnection({ id: crypto.randomUUID(), status: "connected", account_domain: accountDomain, account_id: "demo", client_id: String(data.get("client_id")), redirect_uri: String(data.get("redirect_uri")), token_expires_at: new Date(Date.now() + 86_400_000).toISOString(), connected_at: now, disconnected_at: null, version: 1, created_at: now, updated_at: now });
        setShowConnect(false);
        return;
      }
      const result = await api.post<ApiAmoOAuthStart>("/admin/integrations/amocrm/oauth/start", {
        client_id: String(data.get("client_id")).trim(),
        client_secret: String(data.get("client_secret")),
        redirect_uri: String(data.get("redirect_uri")),
        allowed_referers: [accountDomain],
      });
      if (popup) popup.location.assign(result.authorization_url);
      else setAuthorizationUrl(result.authorization_url);
    } catch (reason) {
      popup?.close();
      oauthWindow.current = null;
      setError(errorMessage(reason, "Не удалось начать OAuth-подключение amoCRM."));
    } finally {
      setSaving(null);
    }
  }

  async function disconnectAmo() {
    if (!connection) return;
    setSaving("disconnect");
    setError("");
    try {
      if (remoteEnabled) {
        await api.post<void>("/admin/integrations/amocrm/disconnect", { expected_version: connection.version });
        await connectionQuery.refetch();
      } else {
        setLocalConnection(null);
      }
      setShowStart(false);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось отключить amoCRM."));
    } finally {
      setSaving(null);
    }
  }

  async function startImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving("start");
    setError("");
    try {
      const userMapping = parseAmoUserMapping(String(data.get("user_mapping") ?? "{}"));
      const payload = { entity_type: String(data.get("entity_type")), dry_run: data.get("dry_run") === "on", user_mapping: userMapping };
      const created = remoteEnabled ? await api.post<ApiImportJob>("/admin/integrations/imports/start", payload) : demoImport(payload.entity_type, payload.dry_run, payload.user_mapping);
      if (remoteEnabled) await importsQuery.refetch(); else setLocalImports((items) => [created, ...items]);
      setShowStart(false);
    } catch (reason) {
      setError(errorMessage(reason, "Не удалось запустить импорт. Проверьте подключение amoCRM."));
    } finally {
      setSaving(null);
    }
  }
  async function importAction(job: ApiImportJob, action: "pause" | "resume") {
    setSaving(job.id);
    setError("");
    try {
      if (remoteEnabled) { await api.post<ApiImportJob>(`/admin/integrations/imports/${job.id}/${action}`, { expected_version: job.version }); await importsQuery.refetch(); }
      else setLocalImports((items) => items.map((item) => item.id === job.id ? { ...item, status: action === "pause" ? "paused" : "running", version: item.version + 1, updated_at: new Date().toISOString() } : item));
    } catch (reason) {
      setError(errorMessage(reason, `Не удалось ${action === "pause" ? "приостановить" : "возобновить"} импорт.`));
    } finally {
      setSaving(null);
    }
  }
  async function retryJob(job: ApiBackgroundJob) {
    setSaving(job.id);
    setError("");
    try { await api.post<ApiBackgroundJob>(`/admin/jobs/${job.id}/retry`, {}); await Promise.all([failedJobsQuery.refetch(), importsQuery.refetch()]); }
    catch (reason) { setError(errorMessage(reason, "Не удалось повторить фоновое задание.")); }
    finally { setSaving(null); }
  }
  async function downloadImportReport(job: ApiImportJob) {
    const reportWindow = window.open("", "_blank");
    if (reportWindow) reportWindow.opener = null;
    setSaving(`report:${job.id}`);
    setError("");
    try {
      const report = await api.get<ApiImportReport>(`/admin/integrations/imports/${job.id}/report`);
      if (reportWindow) reportWindow.location.assign(report.url);
      else {
        const link = document.createElement("a");
        link.href = report.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.click();
      }
    } catch (reason) {
      reportWindow?.close();
      setError(errorMessage(reason, "Не удалось получить ссылку на отчёт импорта."));
    } finally {
      setSaving(null);
    }
  }
  return (
    <>
      <SettingsHeading title="Импорт из amoCRM" />
      <section className="import-panel"><span className="import-panel__icon"><Upload size={28} /></span><div><h3>Разовый безопасный перенос</h3><p>Сначала Pulse CRM проверит доступные сущности и покажет dry-run без записи данных.</p></div><ol><li><Check size={16} /> Воронки, контакты, компании и сделки</li><li><Check size={16} /> Открытые задачи, поля и заметки</li><li><Check size={16} /> Повторный запуск без дублей</li></ol><Button variant="primary" disabled={connectionQuery.isLoading} onClick={() => { if (connected) setShowStart((value) => !value); else setShowConnect((value) => !value); }}><CirclePlay size={16} /> {connected ? "Запустить импорт" : "Подключить amoCRM"}</Button></section>
      {connection ? <section className="amocrm-connection" aria-label="Подключение amoCRM"><span className="operation-icon"><Globe2 size={18} /></span><div><strong>{connection.account_domain}</strong><small>{connected ? `Подключено${connection.token_expires_at ? ` · токен до ${formatDate(connection.token_expires_at)}` : ""}` : "Подключение отключено"}</small></div><StatusPill status={connection.status} /><div className="operation-actions">{connected ? <Button compact onClick={() => void disconnectAmo()} disabled={saving === "disconnect"}>Отключить</Button> : <Button compact onClick={() => setShowConnect(true)}>Подключить снова</Button>}</div></section> : null}
      {connectionQuery.isError ? <SettingsNotice tone="error">Не удалось проверить подключение amoCRM.</SettingsNotice> : null}
      {showConnect ? <SettingsEditor title="OAuth-подключение amoCRM" icon={<Globe2 size={19} />} onCancel={() => setShowConnect(false)}><form onSubmit={(event) => void connectAmo(event)}><div className="settings-form-grid"><label className="field"><span>Integration ID</span><input name="client_id" required autoComplete="off" placeholder="ID внешней интеграции" /></label><label className="field"><span>Secret Key</span><input name="client_secret" type="password" required autoComplete="new-password" placeholder="Секрет интеграции" /></label><label className="field"><span>Домен аккаунта amoCRM</span><input name="account_domain" required inputMode="url" placeholder="example.amocrm.ru" /></label><label className="field"><span>Redirect URI</span><input name="redirect_uri" value={`${window.location.origin}/api/v1/admin/integrations/amocrm/oauth/callback`} readOnly /></label></div><p className="settings-form-help">Добавьте этот Redirect URI в настройках внешней интеграции amoCRM. OAuth работает только на HTTPS-домене Pulse CRM; токены сохраняются в PostgreSQL в зашифрованном виде.</p>{authorizationUrl ? <p className="oauth-fallback">Браузер заблокировал popup. <a href={authorizationUrl} target="pulse-amocrm-oauth" rel="noreferrer">Открыть страницу amoCRM</a></p> : null}{error ? <p className="form-error" role="alert">{error}</p> : null}<EditorActions saving={saving === "connect"} onCancel={() => setShowConnect(false)} submitLabel="Открыть amoCRM" /></form></SettingsEditor> : null}
      {showStart ? <SettingsEditor title="Запуск импорта" icon={<Database size={19} />} onCancel={() => setShowStart(false)}>
        <form onSubmit={(event) => void startImport(event)}>
          <div className="settings-form-grid"><label className="field"><span>Сущности</span><select name="entity_type" defaultValue="all">{importEntities.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
          <label className="field settings-json"><span>Сопоставление пользователей (JSON)</span><textarea name="user_mapping" rows={6} defaultValue="{}" spellCheck={false} aria-describedby="amo-user-mapping-help" /></label>
          <p className="settings-form-help" id="amo-user-mapping-help">Необязательно. Укажите пары «ID пользователя amoCRM»: «UUID пользователя Pulse CRM». Например: {`{"88421":"a0ebc999-9c0b-4ef8-9e6a-684fe6772c9d"}`}.</p>
          <div className="settings-checks"><label><input name="dry_run" type="checkbox" defaultChecked /> Dry-run без записи бизнес-данных</label></div>
          <p className="settings-form-help">Режим «все сущности» переносит данные в безопасной последовательности небольшими страницами. Задание продолжится после перезапуска webapp и может быть приостановлено.</p>
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <EditorActions saving={saving === "start"} onCancel={() => setShowStart(false)} submitLabel="Запустить" />
        </form>
      </SettingsEditor> : null}
      {error && !showStart && !showConnect ? <SettingsNotice tone="error">{error}</SettingsNotice> : null}
      <section className="settings-subsection"><header><div><h3>Запуски импорта</h3><p>Статус, прогресс и управление возобновлением.</p></div><Button compact onClick={() => void importsQuery.refetch()} disabled={!remoteEnabled || importsQuery.isFetching}><RefreshCcw size={15} /> Обновить</Button></header><div className="operation-list">{imports.map((job) => <article key={job.id}><span className="operation-icon"><Database size={18} /></span><div><strong>{importEntityLabel(job.entity_type)} {job.dry_run ? "· dry-run" : "· перенос"}</strong><small>{countSummary(job.counts)}{Object.keys(job.user_mapping).length ? ` · ${Object.keys(job.user_mapping).length} сопоставлено` : ""} · обновлено {formatDate(job.updated_at)}</small>{job.last_error ? <em>{job.last_error}</em> : null}</div><StatusPill status={job.status} /><div className="operation-actions">{job.status === "running" || job.status === "pending" ? <Button compact onClick={() => void importAction(job, "pause")} disabled={saving === job.id}><Pause size={14} /> Пауза</Button> : null}{job.status === "paused" || job.status === "failed" ? <Button compact onClick={() => void importAction(job, "resume")} disabled={saving === job.id}><Play size={14} /> Продолжить</Button> : null}{job.status === "succeeded" && job.report_object_key ? <Button compact onClick={() => void downloadImportReport(job)} disabled={saving === `report:${job.id}`}><Upload size={14} /> Скачать отчёт</Button> : null}</div></article>)}{!imports.length && !importsQuery.isLoading ? <SettingsEmpty icon={<Database size={22} />} title="Импорт ещё не запускался" text="Начните с dry-run воронок, затем переносите связанные сущности." /> : null}</div></section>
      <section className="settings-subsection"><header><div><h3>Ошибки фоновых заданий</h3><p>После исправления причины задание можно безопасно повторить.</p></div></header><div className="operation-list operation-list--failed">{failedJobs.map((job) => <article key={job.id}><span className="operation-icon operation-icon--danger"><AlertTriangle size={18} /></span><div><strong>{job.job_type}</strong><small>{job.attempts} из {job.max_attempts} попыток · {formatDate(job.updated_at)}</small><em>{job.last_error ?? "Причина не записана"}</em></div><StatusPill status="failed" /><div className="operation-actions"><Button compact onClick={() => void retryJob(job)} disabled={saving === job.id}><RotateCcw size={14} /> Повторить</Button></div></article>)}{!failedJobs.length && !failedJobsQuery.isLoading ? <SettingsEmpty icon={<Check size={22} />} title="Ошибок нет" text="Все фоновые задания выполняются штатно." /> : null}</div></section>
    </>
  );
}

function InviteUsersPanel() {
  const { session } = useAuth();
  const [invite, setInvite] = useState<InvitationCreated | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const usersQuery = useQuery({ queryKey: ["users"], queryFn: () => api.get<ApiUser[]>("/users"), enabled: remoteEnabled });
  const members = usersQuery.data ?? (session ? [session.user] : []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving(true);
    setError("");
    try {
      if (!remoteEnabled) { setInvite({ id: crypto.randomUUID(), email: String(data.get("email")), role: String(data.get("role")) as "admin" | "manager", expires_at: new Date(Date.now() + 72 * 3_600_000).toISOString(), token: "demo-invitation-token" }); return; }
      setInvite(await api.post<InvitationCreated>("/invitations", { email: String(data.get("email")), role: String(data.get("role")) }));
    } catch { setError("Не удалось создать приглашение"); }
    finally { setSaving(false); }
  }
  return <><SettingsHeading title="Пользователи и роли" /><section className="members-panel"><div className="members-list">{members.map((member) => <div key={member.id}><span className="company-avatar">{member.full_name.slice(0, 1)}</span><span><strong>{member.full_name}</strong><small>{member.email}</small></span><em>{member.role === "owner" ? "Владелец" : member.role === "admin" ? "Администратор" : "Менеджер"}</em></div>)}</div><form className="invite-form" onSubmit={(event) => void submit(event)}><header><UserPlus size={19} /><div><strong>Пригласить сотрудника</strong><small>Ссылка действует 72 часа</small></div></header><label className="field"><span>Email</span><input name="email" type="email" required placeholder="manager@company.ru" /></label><label className="field"><span>Роль</span><select name="role" defaultValue="manager"><option value="manager">Менеджер</option>{session?.user.role === "owner" ? <option value="admin">Администратор</option> : null}</select></label>{error ? <p className="form-error" role="alert">{error}</p> : null}<Button type="submit" variant="primary" disabled={saving}>{saving ? "Создаём…" : "Создать приглашение"}</Button>{invite ? <div className="invite-result" role="status"><strong>Приглашение готово</strong><code>{`${window.location.origin}/accept-invitation?token=${invite.token}`}</code></div> : null}</form></section></>;
}

function SettingsHeading({ title, action, onAction }: { title: string; action?: string; onAction?: () => void }) {
  return <header className="settings-heading"><div><h2>{title}</h2><p>Изменения применяются ко всей компании.</p></div>{action ? <Button variant="primary" onClick={onAction}><Plus size={16} /> {action}</Button> : null}</header>;
}
function SettingsEditor({ title, icon, onCancel, children }: { title: string; icon: ReactNode; onCancel: () => void; children: ReactNode }) {
  return <section className="settings-editor"><header><span>{icon}</span><strong>{title}</strong><button className="icon-button" type="button" aria-label="Закрыть форму" onClick={onCancel}><X size={18} /></button></header>{children}</section>;
}
function EditorActions({ saving, submitLabel, onCancel }: { saving: boolean; submitLabel: string; onCancel: () => void }) {
  return <div className="settings-editor__actions"><Button type="button" onClick={onCancel}>Отмена</Button><Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : submitLabel}</Button></div>;
}
function SettingsNotice({ tone, children }: { tone: "neutral" | "error"; children: ReactNode }) {
  return <div className={`settings-notice settings-notice--${tone}`} role={tone === "error" ? "alert" : "status"}>{children}</div>;
}
function SettingsEmpty({ icon, title, text }: { icon: ReactNode; title: string; text: string }) {
  return <div className="settings-empty"><span>{icon}</span><div><strong>{title}</strong><small>{text}</small></div></div>;
}
function StatusPill({ status }: { status: string }) {
  const labels: Record<string, string> = { active: "Активно", disabled: "Выключено", degraded: "Требует настройки", pending: "Ожидает", running: "Выполняется", paused: "На паузе", succeeded: "Завершено", failed: "Ошибка" };
  return <em className={`status-pill status-pill--${status}`}>{labels[status] ?? status}</em>;
}

function channelLabel(kind: ApiChannelKind): string { return { email: "IMAP/SMTP", telegram: "Telegram Bot API", max: "MAX Bot API" }[kind]; }
function notificationChannelLabel(channel: ApiNotificationChannel): string { return { in_app: "в приложении", email: "email", telegram: "Telegram", max: "MAX" }[channel]; }
function routeLabel(stageId: string | null, pipeline: Pipeline): string { return pipeline.stages.find((stage) => stage.id === stageId)?.name ?? "маршрут не задан"; }
function formatDate(value: string): string { return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function errorMessage(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return reason instanceof Error && reason.message ? reason.message : fallback;
  const details = reason.details;
  if (details && typeof details === "object" && "detail" in details) {
    const detail = (details as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail && typeof (detail as { message?: unknown }).message === "string") return (detail as { message: string }).message;
  }
  return fallback;
}
function credentialsExample(kind: ApiChannelKind): string {
  const value = kind === "email" ? { smtp: { host: "smtp.example.ru", port: 587, security: "starttls", username: "sales@example.ru", password: "", from_address: "sales@example.ru" }, imap: { host: "imap.example.ru", port: 993, security: "ssl", username: "sales@example.ru", password: "", mailbox: "INBOX" } } : kind === "telegram" ? { bot_token: "", webhook_secret: "" } : { access_token: "", webhook_secret: "" };
  return JSON.stringify(value, null, 2);
}
function demoChannel(kind: ApiChannelKind, name: string, pipelineId: string, stageId: string): ApiChannelConnection { const now = new Date().toISOString(); return { id: crypto.randomUUID(), kind, name, status: "active", settings: {}, default_pipeline_id: pipelineId, default_stage_id: stageId, default_assignee_id: null, has_credentials: true, last_healthcheck_at: null, last_error: null, version: 1, created_at: now, updated_at: now }; }
function demoWebhook(name: string, slug: string, pipelineId: string, stageId: string): ApiWebhookEndpoint { const now = new Date().toISOString(); return { id: crypto.randomUUID(), name, slug, pipeline_id: pipelineId, stage_id: stageId, assignee_id: null, source_id: null, is_active: true, version: 1, created_at: now, updated_at: now }; }
function demoForm(title: string, slug: string, pipelineId: string, stageId: string, origins: string[]): ApiHtmlForm { const now = new Date().toISOString(); return { id: crypto.randomUUID(), title, slug, pipeline_id: pipelineId, stage_id: stageId, assignee_id: null, source_id: null, fields_schema: [], allowed_origins: origins, honeypot_field: "company_website", success_message: "Спасибо! Мы свяжемся с вами.", is_active: true, version: 1, created_at: now, updated_at: now }; }
function demoTemplate(name: string, channel: ApiNotificationChannel, body: string): ApiNotificationTemplate { const now = new Date().toISOString(); return { id: crypto.randomUUID(), name, channel, subject_template: null, body_template: body, is_active: true, version: 1, created_at: now, updated_at: now }; }
function demoCustomField(payload: { entity_type: "deal"; key: string; name: string; field_type: ApiCustomFieldType; options: string[] }): ApiCustomField { return { id: crypto.randomUUID(), is_active: true, ...payload }; }
function demoRequiredFields(fields: Array<{ built_in_key: string } | { field_definition_id: string }>): ApiRequiredField[] { return fields.map((field) => ({ id: crypto.randomUUID(), built_in_key: "built_in_key" in field ? field.built_in_key : null, field_definition_id: "field_definition_id" in field ? field.field_definition_id : null })); }
function demoImport(entityType: string, dryRun: boolean, userMapping: Record<string, string>): ApiImportJob { const now = new Date().toISOString(); return { id: crypto.randomUUID(), provider: "amocrm", status: "running", dry_run: dryRun, entity_type: entityType, cursor: {}, user_mapping: userMapping, counts: {}, report_object_key: null, started_at: now, completed_at: null, last_error: null, version: 1, created_at: now, updated_at: now }; }
function pipelineToApi(pipeline: Pipeline): ApiPipeline { return { id: pipeline.id, name: pipeline.name, position: 0, is_active: true, version: 1, stages: pipeline.stages.map((stage, position) => ({ id: stage.id, pipeline_id: pipeline.id, name: stage.name, color: stage.color === "blue" ? "#4B96F8" : stage.color === "violet" ? "#6D5DF7" : stage.color === "green" ? "#20B878" : "#F5A313", position, stage_type: stage.stageType ?? "open" })) }; }
function demoPipeline(name: string, position: number): ApiPipeline { const id = crypto.randomUUID(); const stages = [{ name: "Новый лид", color: "#4B96F8", stage_type: "open" as const }, { name: "Связались", color: "#6D5DF7", stage_type: "open" as const }, { name: "Предложение", color: "#20B878", stage_type: "open" as const }, { name: "Успешно реализовано", color: "#16A36D", stage_type: "won" as const }, { name: "Закрыто и не реализовано", color: "#929AAA", stage_type: "lost" as const }]; return { id, name, position, is_active: true, version: 1, stages: stages.map((stage, stagePosition) => ({ id: crypto.randomUUID(), pipeline_id: id, position: stagePosition, ...stage })) }; }
function countSummary(counts: Record<string, number>): string { const total = Object.values(counts).reduce((sum, value) => sum + value, 0); return total ? `${total.toLocaleString("ru-RU")} обработано` : "ожидает первую страницу"; }
function importEntityLabel(entityType: string | null): string { return importEntities.find(([value]) => value === entityType)?.[1] ?? entityType ?? "Все сущности"; }
function fieldTypeLabel(fieldType: ApiCustomFieldType): string { return { text: "Текст", number: "Число", date: "Дата", boolean: "Флаг", select: "Список" }[fieldType]; }

const notificationEvents: [string, string][] = [["lead.created", "Новый лид"], ["deal.assigned", "Назначение сделки"], ["message.inbound.received", "Новое входящее сообщение"], ["task.due_soon", "Приближается срок задачи"], ["task.overdue", "Задача просрочена"], ["deal.inactive", "Нет активности в сделке"], ["purchase.due_soon", "Приближается следующая покупка"], ["deal.stage_changed", "Изменение этапа сделки"]];
const importEntities: [string, string][] = [["all", "Все сущности по порядку"], ["pipelines", "1. Воронки"], ["stages", "2. Этапы"], ["users", "3. Пользователи"], ["custom_fields", "4. Пользовательские поля"], ["companies", "5. Компании"], ["contacts", "6. Контакты"], ["deals", "7. Сделки"], ["tasks", "8. Открытые задачи"], ["notes", "9. Обычные заметки"]];
