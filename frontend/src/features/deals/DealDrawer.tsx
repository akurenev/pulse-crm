import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarDays,
  Building2,
  Check,
  ChevronDown,
  Circle,
  Mail,
  Paperclip,
  Pencil,
  Phone,
  Send,
  Tag,
  Trash2,
  UserRound,
  UserRoundCheck,
  X,
} from "lucide-react";
import { useDeferredValue, useEffect, useId, useState, type FormEvent } from "react";

import { Avatar } from "../../components/Avatar";
import { Button } from "../../components/Button";
import { ApiError, api, remoteEnabled } from "../../lib/api";
import { parseDealTags } from "../../lib/deal-tags";
import { formatLongDate, formatMoney, formatTime } from "../../lib/format";
import { DealMutationInProgressError } from "../../state/crm-store";
import type { ApiActivity, ApiCompany, ApiContact, ApiCustomField, CursorPage } from "../../types/api";
import type { Deal, Pipeline, UserSummary } from "../../types/crm";

interface DealDrawerProps {
  deal: Deal | null;
  pipeline: Pipeline;
  assignees: UserSummary[];
  mutationPending: boolean;
  onClose: () => void;
  onMove: (dealId: string, stageId: string) => Promise<void>;
  onSetNextPurchase: (dealId: string, date: string | null) => Promise<void>;
  onSetContact: (dealId: string, contact: { id: string; name: string; phone?: string; email?: string } | null) => Promise<void>;
  onSetCompany: (dealId: string, company: { id: string; name: string } | null) => Promise<void>;
  onSetAssignee: (dealId: string, assignee: UserSummary | null) => Promise<void>;
  onSetTags: (dealId: string, tags: string[]) => Promise<void>;
  onSetCustomFields: (dealId: string, fields: Record<string, unknown>) => Promise<void>;
  onSendMessage: (dealId: string, body: string, attachment?: File) => Promise<void>;
  onRetryMessage: (dealId: string, messageId: string) => Promise<void>;
  onToggleTask: (dealId: string, taskId: string) => Promise<void>;
  onDelete: (dealId: string) => Promise<void>;
}

export function DealDrawer({ deal, pipeline, assignees, mutationPending, onClose, onMove, onSetNextPurchase, onSetContact, onSetCompany, onSetAssignee, onSetTags, onSetCustomFields, onSendMessage, onRetryMessage, onToggleTask, onDelete }: DealDrawerProps) {
  const queryClient = useQueryClient();
  const mobileModal = useMediaQuery("(max-width: 720px)");
  const [tab, setTab] = useState("details");
  const [messageError, setMessageError] = useState("");
  const [noteError, setNoteError] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [stageError, setStageError] = useState("");
  const historyQuery = useQuery({
    queryKey: ["activity", "deal", deal?.id],
    queryFn: () => api.get<CursorPage<ApiActivity>>(`/activity?limit=100&entity_type=deal&entity_id=${deal?.id}`),
    enabled: remoteEnabled && Boolean(deal?.id),
  });
  useEffect(() => {
    setDeleteConfirmOpen(false);
    setDeleteError("");
    setStageError("");
  }, [deal?.id]);
  if (!deal) return null;

  async function handleMove(stageId: string) {
    if (!deal) return;
    setStageError("");
    try {
      await onMove(deal.id, stageId);
    } catch (reason) {
      setStageError(dealMutationErrorMessage(reason, "Не удалось изменить этап сделки."));
    }
  }

  async function handleMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const body = String(data.get("message") ?? "").trim();
    const attachment = data.get("attachment");
    const file = attachment instanceof File && attachment.size ? attachment : undefined;
    if ((!body && !file) || !deal) return;
    if (file && file.size > 20 * 1024 * 1024) {
      setMessageError("Файл превышает лимит 20 МБ");
      return;
    }
    setMessageError("");
    try {
      await onSendMessage(deal.id, body || `Вложение: ${file?.name ?? "файл"}`, file);
      form.reset();
    } catch {
      setMessageError("Сообщение не сохранено. Проверьте канал и формат вложения.");
    }
  }

  async function handleNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = String(new FormData(form).get("note") ?? "").trim();
    if (!body || !deal) return;
    if (!remoteEnabled) {
      setNoteError("Заметки сохраняются после подключения API");
      return;
    }
    setNoteSaving(true);
    setNoteError("");
    try {
      await api.post<ApiActivity>(`/deals/${deal.id}/notes`, { body });
      form.reset();
      await queryClient.invalidateQueries({ queryKey: ["activity", "deal", deal.id] });
    } catch {
      setNoteError("Не удалось сохранить заметку");
    } finally {
      setNoteSaving(false);
    }
  }

  async function handleDelete() {
    if (!deal) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await onDelete(deal.id);
      setDeleteConfirmOpen(false);
    } catch (reason) {
      setDeleteError(dealMutationErrorMessage(reason, "Не удалось удалить сделку. Повторите попытку."));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <>
      <Dialog.Root open modal={mobileModal && !deleteConfirmOpen} onOpenChange={(open) => { if (!open && !deleteConfirmOpen) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content className="deal-drawer" aria-describedby={undefined} aria-busy={mutationPending}>
          <header className="deal-drawer__header">
            <div className="deal-drawer__identity">
              <Dialog.Title>{deal.title}</Dialog.Title>
              <strong>{formatMoney(deal.amount)}</strong>
            </div>
            <label className="stage-select deal-drawer__stage">
              <span className="sr-only">Этап сделки</span>
              <select value={deal.stageId} disabled={mutationPending} onChange={(event) => void handleMove(event.target.value)}>
                {pipeline.stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.name}</option>)}
              </select>
            </label>
            <div className="deal-drawer__header-actions">
              <button
                type="button"
                className="icon-button deal-drawer__delete"
                aria-label="Удалить сделку"
                title="Удалить сделку"
                disabled={mutationPending}
                onClick={() => { setDeleteError(""); setDeleteConfirmOpen(true); }}
              >
                <Trash2 size={18} aria-hidden="true" />
              </button>
              <Dialog.Close className="icon-button deal-drawer__close" aria-label="Закрыть карточку">
                <X size={19} />
              </Dialog.Close>
            </div>
          </header>
          {stageError ? <p className="form-error" role="alert">{stageError}</p> : null}

          <Tabs.Root value={tab} onValueChange={setTab} className="deal-tabs">
            <Tabs.List aria-label="Разделы сделки">
              <Tabs.Trigger value="details">Детали</Tabs.Trigger>
              <Tabs.Trigger value="tasks">Задачи</Tabs.Trigger>
              <Tabs.Trigger value="messages">Переписка{deal.messages.length ? <span>{deal.messages.length}</span> : null}</Tabs.Trigger>
              <Tabs.Trigger value="history">История</Tabs.Trigger>
            </Tabs.List>

            <div className="deal-drawer__scroll">
              <Tabs.Content value="details" className="drawer-section">
                <dl className="details-list deal-details">
                  <div className="deal-details__row deal-details__row--contact">
                    <dt className="deal-details__label"><UserRound size={17} aria-hidden="true" /><span className="deal-details__label-text">Контакт</span></dt>
                    <dd className="deal-details__value"><ContactPicker deal={deal} disabled={mutationPending} onSave={onSetContact} /></dd>
                  </div>
                  {deal.phone ? <div className="deal-details__row deal-details__row--phone"><dt className="deal-details__label"><Phone size={17} aria-hidden="true" /><span className="deal-details__label-text">Телефон</span></dt><dd className="deal-details__value"><a href={`tel:${deal.phone}`}>{deal.phone}</a></dd></div> : null}
                  {deal.email ? <div className="deal-details__row deal-details__row--email"><dt className="deal-details__label"><Mail size={17} aria-hidden="true" /><span className="deal-details__label-text">Email</span></dt><dd className="deal-details__value"><a href={`mailto:${deal.email}`}>{deal.email}</a></dd></div> : null}
                  <div className="deal-details__row deal-details__row--company">
                    <dt className="deal-details__label"><Building2 size={17} aria-hidden="true" /><span className="deal-details__label-text">Компания</span></dt>
                    <dd className="deal-details__value"><CompanyPicker deal={deal} disabled={mutationPending} onSave={onSetCompany} /></dd>
                  </div>
                  <div className="deal-details__row deal-details__row--owner">
                    <dt className="deal-details__label"><UserRoundCheck size={17} aria-hidden="true" /><span className="deal-details__label-text">Ответственный</span></dt>
                    <dd className="deal-details__value"><AssigneePicker key={deal.id} deal={deal} assignees={assignees} disabled={mutationPending} onSave={onSetAssignee} /></dd>
                  </div>
                  <div className="deal-details__row deal-details__row--tags">
                    <dt className="deal-details__label"><Tag size={17} aria-hidden="true" /><span className="deal-details__label-text">Теги</span></dt>
                    <dd className="deal-details__value"><DealTagsEditor key={deal.id} deal={deal} disabled={mutationPending} onSave={onSetTags} /></dd>
                  </div>
                  <div className="deal-details__row deal-details__row--next-purchase">
                    <dt className="deal-details__label"><CalendarDays size={17} aria-hidden="true" /><span className="deal-details__label-text">Следующая покупка</span></dt>
                    <dd className="deal-details__value"><NextPurchaseEditor key={deal.id} deal={deal} disabled={mutationPending} onSave={onSetNextPurchase} /></dd>
                  </div>
                </dl>
                <CustomFieldsEditor key={deal.id} deal={deal} disabled={mutationPending} onSave={onSetCustomFields} />
                <DrawerTasks deal={deal} onToggleTask={onToggleTask} />
                <DrawerMessages deal={deal} onSubmit={handleMessage} onRetryMessage={onRetryMessage} error={messageError} />
              </Tabs.Content>

              <Tabs.Content value="tasks" className="drawer-section">
                <DrawerTasks deal={deal} onToggleTask={onToggleTask} expanded />
              </Tabs.Content>

              <Tabs.Content value="messages" className="drawer-section drawer-section--messages">
                <DrawerMessages deal={deal} onSubmit={handleMessage} onRetryMessage={onRetryMessage} error={messageError} expanded />
              </Tabs.Content>

              <Tabs.Content value="history" className="drawer-section">
                <form className="note-composer" onSubmit={(event) => void handleNote(event)}>
                  <textarea name="note" rows={3} maxLength={10_000} placeholder="Добавить заметку к сделке" aria-label="Текст заметки" />
                  <button type="submit" disabled={noteSaving}>{noteSaving ? "Сохраняем…" : "Добавить заметку"}</button>
                  {noteError ? <small className="message-error" role="alert">{noteError}</small> : null}
                </form>
                {historyQuery.isLoading ? <p className="empty-copy timeline-loading">Загружаем историю…</p> : null}
                <ol className="timeline">
                  {remoteEnabled ? (historyQuery.data?.items ?? []).map((event) => (
                    <li key={event.id}><span /><div><strong>{activityTitle(event.event_type)}</strong><p>{activityDetail(event)}</p><time>{formatActivityDate(event.occurred_at)}</time></div></li>
                  )) : <>
                    <li><span /><div><strong>Сделка создана</strong><p>Источник: {deal.sourceLabel}</p><time>Сегодня</time></div></li>
                    <li><span /><div><strong>Назначен ответственный</strong><p>{deal.assignee.name}</p><time>Сегодня</time></div></li>
                    <li><span /><div><strong>Этап изменён</strong><p>{pipeline.stages.find((stage) => stage.id === deal.stageId)?.name}</p><time>Сегодня</time></div></li>
                  </>}
                </ol>
                {remoteEnabled && !historyQuery.isLoading && !historyQuery.data?.items.length ? <p className="empty-copy">История пока пуста</p> : null}
              </Tabs.Content>
            </div>
          </Tabs.Root>
        </Dialog.Content>
      </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={deleteConfirmOpen}
        onOpenChange={(open) => {
          if (!deleting) {
            setDeleteConfirmOpen(open);
            if (!open) setDeleteError("");
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay dialog-overlay--confirm" />
          <Dialog.Content className="dialog-content confirm-dialog">
            <div className="dialog-header">
              <div>
                <Dialog.Title>Удалить сделку?</Dialog.Title>
                <Dialog.Description>«{deal.title}» будет удалена из CRM. Это действие нельзя отменить.</Dialog.Description>
              </div>
              <Dialog.Close className="icon-button" aria-label="Закрыть подтверждение" disabled={deleting}><X size={20} /></Dialog.Close>
            </div>
            {deleteError ? <p className="form-error" role="alert">{deleteError}</p> : null}
            <div className="dialog-actions">
              <Dialog.Close asChild><Button type="button" disabled={deleting}>Отмена</Button></Dialog.Close>
              <Button type="button" variant="danger" disabled={deleting || mutationPending} onClick={() => void handleDelete()}>{deleting ? "Удаляем…" : "Удалить сделку"}</Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

const activityLabels: Record<string, string> = {
  "deal.created": "Сделка создана",
  "deal.updated": "Сделка обновлена",
  "deal.assigned": "Назначен ответственный",
  "deal.stage_changed": "Этап изменён",
  "deal.note.created": "Заметка",
  "message.inbound.received": "Получено сообщение",
  "message.outbound.queued": "Сообщение поставлено в очередь",
};

function activityTitle(eventType: string) {
  return activityLabels[eventType] ?? eventType.replaceAll(".", " · ");
}

function activityDetail(event: ApiActivity) {
  if (typeof event.payload.body === "string") return event.payload.body;
  if (Array.isArray(event.payload.fields)) return `Поля: ${event.payload.fields.join(", ")}`;
  if (typeof event.payload.title === "string") return event.payload.title;
  return "Событие записано в журнал";
}

function formatActivityDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia(query).matches);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", update);
      return () => media.removeEventListener("change", update);
    }
    media.addListener(update);
    return () => media.removeListener(update);
  }, [query]);

  return matches;
}

function dealMutationErrorMessage(reason: unknown, fallback: string) {
  if (reason instanceof DealMutationInProgressError) return reason.message;
  if (!(reason instanceof ApiError)) return fallback;
  if (reason.status === 404) return "Сделка уже удалена другим пользователем.";
  if (reason.status === 409) return "Сделка уже изменена другим пользователем. Данные обновлены — повторите действие.";
  const detail = reason.details && typeof reason.details === "object" && "detail" in reason.details
    ? (reason.details as { detail?: unknown }).detail
    : undefined;
  if (detail && typeof detail === "object" && "code" in detail && detail.code === "missing_required_fields") {
    const fields = "fields" in detail && Array.isArray(detail.fields)
      ? detail.fields.map((field) => {
        if (!field || typeof field !== "object") return "";
        if ("name" in field && field.name) return String(field.name);
        return "key" in field && field.key ? String(field.key) : "";
      }).filter(Boolean)
      : [];
    return fields.length ? `Заполните обязательные поля: ${fields.join(", ")}.` : "Заполните обязательные поля этапа.";
  }
  return fallback;
}

function AssigneePicker({ deal, assignees, disabled, onSave }: {
  deal: Deal;
  assignees: UserSummary[];
  disabled: boolean;
  onSave: DealDrawerProps["onSetAssignee"];
}) {
  const [editing, setEditing] = useState(false);
  const [selectedId, setSelectedId] = useState(deal.assignee.id === "unassigned" ? "" : deal.assignee.id);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  function startEditing() {
    setSelectedId(deal.assignee.id === "unassigned" ? "" : deal.assignee.id);
    setError("");
    setEditing(true);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const assignee = assignees.find((user) => user.id === selectedId) ?? null;
    setSaving(true);
    setError("");
    try {
      await onSave(deal.id, assignee);
      setEditing(false);
    } catch (reason) {
      setError(dealMutationErrorMessage(reason, "Не удалось изменить ответственного сделки."));
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return <span className="relation-value owner-value"><span className="owner-line"><Avatar user={deal.assignee} size="sm" /><span>{deal.assignee.name}</span></span><EditFieldButton label="Изменить ответственного сделки" disabled={disabled} onClick={startEditing} /></span>;
  }

  return <form className="owner-picker" onSubmit={(event) => void submit(event)}>
    <select autoFocus aria-label="Ответственный сделки" value={selectedId} onChange={(event) => setSelectedId(event.target.value)} disabled={saving || disabled}>
      <option value="">Не назначен</option>
      {assignees.map((assignee) => <option key={assignee.id} value={assignee.id}>{assignee.name}</option>)}
    </select>
    <span className="owner-picker__actions">
      <button type="button" disabled={saving} onClick={() => setEditing(false)}>Отмена</button>
      <button type="submit" aria-label="Сохранить ответственного сделки" disabled={saving || disabled}>{saving ? "Сохраняем…" : "Сохранить"}</button>
    </span>
    {error ? <small className="message-error" role="alert">{error}</small> : null}
  </form>;
}

function ContactPicker({ deal, disabled, onSave }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetContact"] }) {
  const [editing, setEditing] = useState(false);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const results = useQuery({
    queryKey: ["contact-picker", deferredSearch],
    queryFn: () => api.get<CursorPage<ApiContact>>(`/contacts?limit=20&search=${encodeURIComponent(deferredSearch)}`),
    enabled: remoteEnabled && editing && deferredSearch.length >= 2,
  });

  async function choose(contact: ApiContact | null) {
    setSaving(true);
    setError("");
    try {
      await onSave(deal.id, contact ? {
        id: contact.id,
        name: `${contact.first_name} ${contact.last_name}`.trim(),
        phone: contact.primary_phone ?? contact.phones[0] ?? undefined,
        email: contact.primary_email ?? contact.emails[0] ?? undefined,
      } : null);
      setEditing(false);
      setSearch("");
    } catch {
      setError("Не удалось изменить контакт");
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return <span className="relation-value"><span>{deal.contactName ?? "Не указан"}</span>{remoteEnabled ? <EditFieldButton label="Изменить контакт сделки" disabled={disabled} onClick={() => setEditing(true)} /> : null}</span>;
  }
  return <span className="relation-picker">
    <input autoFocus aria-label="Поиск контакта" value={search} disabled={disabled} onChange={(event) => setSearch(event.target.value)} placeholder="Имя, телефон или email" />
    {results.isLoading ? <small>Ищем…</small> : null}
    {deferredSearch.length < 2 ? <small>Введите минимум 2 символа</small> : null}
    {(results.data?.items ?? []).map((contact) => <button type="button" key={contact.id} disabled={saving || disabled} onClick={() => void choose(contact)}><strong>{`${contact.first_name} ${contact.last_name}`.trim()}</strong><small>{contact.primary_phone ?? contact.primary_email ?? "Без контактов"}</small></button>)}
    <span className="relation-picker__actions">{deal.contactIds?.length ? <button type="button" disabled={saving || disabled} onClick={() => void choose(null)}>Очистить</button> : null}<button type="button" disabled={saving} onClick={() => setEditing(false)}>Отмена</button></span>
    {error ? <small className="message-error" role="alert">{error}</small> : null}
  </span>;
}

function CompanyPicker({ deal, disabled, onSave }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetCompany"] }) {
  const [editing, setEditing] = useState(false);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const results = useQuery({
    queryKey: ["company-picker", deferredSearch],
    queryFn: () => api.get<CursorPage<ApiCompany>>(`/companies?limit=20&search=${encodeURIComponent(deferredSearch)}`),
    enabled: remoteEnabled && editing && deferredSearch.length >= 2,
  });

  async function choose(company: ApiCompany | null) {
    setSaving(true);
    setError("");
    try {
      await onSave(deal.id, company ? { id: company.id, name: company.name } : null);
      setEditing(false);
      setSearch("");
    } catch {
      setError("Не удалось изменить компанию");
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return <span className="relation-value"><span>{deal.companyName ?? "Не указана"}</span>{remoteEnabled ? <EditFieldButton label="Изменить компанию сделки" disabled={disabled} onClick={() => setEditing(true)} /> : null}</span>;
  }
  return <span className="relation-picker">
    <input autoFocus aria-label="Поиск компании" value={search} disabled={disabled} onChange={(event) => setSearch(event.target.value)} placeholder="Название, телефон или email" />
    {results.isLoading ? <small>Ищем…</small> : null}
    {deferredSearch.length < 2 ? <small>Введите минимум 2 символа</small> : null}
    {(results.data?.items ?? []).map((company) => <button type="button" key={company.id} disabled={saving || disabled} onClick={() => void choose(company)}><strong>{company.name}</strong><small>{company.phone ?? company.email ?? "Без контактов"}</small></button>)}
    <span className="relation-picker__actions">{deal.companyId ? <button type="button" disabled={saving || disabled} onClick={() => void choose(null)}>Очистить</button> : null}<button type="button" disabled={saving} onClick={() => setEditing(false)}>Отмена</button></span>
    {error ? <small className="message-error" role="alert">{error}</small> : null}
  </span>;
}

function DealTagsEditor({ deal, disabled, onSave }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetTags"] }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(deal.tags.join(", "));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  function startEditing() {
    setDraft(deal.tags.join(", "));
    setError("");
    setEditing(true);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await onSave(deal.id, parseDealTags(draft));
      setEditing(false);
    } catch {
      setError("Не удалось сохранить теги сделки");
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return <span className="deal-tags-editor__display"><span className="deal-tags-list">{deal.tags.length ? deal.tags.map((tag) => <span className="deal-tag" key={tag}>{tag}</span>) : <small>Нет тегов</small>}</span><EditFieldButton label="Изменить теги сделки" disabled={disabled} onClick={startEditing} /></span>;
  }

  return <form className="deal-tags-editor" onSubmit={(event) => void submit(event)}>
    <input autoFocus aria-label="Теги сделки" value={draft} disabled={disabled} onChange={(event) => setDraft(event.target.value)} maxLength={10_000} placeholder="VIP, Повторная покупка" />
    <small>Разделяйте теги запятыми</small>
    <span><button type="button" disabled={saving} onClick={() => setEditing(false)}>Отмена</button><button type="submit" aria-label="Сохранить теги сделки" disabled={saving || disabled}>{saving ? "Сохраняем…" : "Сохранить"}</button></span>
    {error ? <small className="message-error" role="alert">{error}</small> : null}
  </form>;
}

function EditFieldButton({ label, disabled = false, onClick }: { label: string; disabled?: boolean; onClick: () => void }) {
  return (
    <button type="button" className="deal-field-edit icon-button" aria-label={label} title={label} disabled={disabled} onClick={onClick}>
      <Pencil size={15} aria-hidden="true" />
    </button>
  );
}

function CustomFieldsEditor({ deal, disabled, onSave }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetCustomFields"] }) {
  const definitions = useQuery({
    queryKey: ["deal-custom-fields"],
    queryFn: () => api.get<ApiCustomField[]>("/custom-fields?entity_type=deal"),
    enabled: remoteEnabled,
  });
  const [draft, setDraft] = useState<Record<string, unknown>>(() => ({ ...(deal.customFields ?? {}) }));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(true);
  const contentId = useId();

  if (!remoteEnabled) return null;
  if (definitions.isLoading) return <section className="custom-fields-editor"><p className="empty-copy">Загружаем поля сделки…</p></section>;
  if (definitions.isError) return <section className="custom-fields-editor"><p className="message-error">Не удалось загрузить пользовательские поля</p></section>;
  if (!definitions.data?.length) return null;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    const normalized = { ...(deal.customFields ?? {}) };
    for (const definition of definitions.data ?? []) {
      const value = draft[definition.key];
      if (definition.field_type === "number") {
        normalized[definition.key] = value === "" || value == null ? null : Number(value);
      } else if (definition.field_type === "boolean") {
        normalized[definition.key] = Boolean(value);
      } else {
        normalized[definition.key] = value === "" ? null : value;
      }
    }
    try {
      await onSave(deal.id, normalized);
    } catch {
      setError("Не удалось сохранить поля сделки");
    } finally {
      setSaving(false);
    }
  }

  return <section className={`custom-fields-editor${expanded ? " custom-fields-editor--expanded" : " custom-fields-editor--collapsed"}`}>
    <header className="custom-fields-editor__header">
      <h3>
        <button
          type="button"
          className="custom-fields-editor__toggle"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>Поля сделки</span>
          <ChevronDown className={`custom-fields-editor__chevron${expanded ? " custom-fields-editor__chevron--expanded" : ""}`} size={18} aria-hidden="true" />
        </button>
      </h3>
    </header>
    <div id={contentId} className="custom-fields-editor__content" hidden={!expanded}>
      <form onSubmit={(event) => void submit(event)}>
        {(definitions.data ?? []).map((definition) => <label className="field custom-fields-editor__field" key={definition.id}>
          <span className="custom-fields-editor__label">{definition.name}</span>
          {definition.field_type === "boolean" ? <input type="checkbox" checked={Boolean(draft[definition.key])} disabled={disabled} onChange={(event) => setDraft((current) => ({ ...current, [definition.key]: event.target.checked }))} />
            : definition.field_type === "select" ? <select value={String(draft[definition.key] ?? "")} disabled={disabled} onChange={(event) => setDraft((current) => ({ ...current, [definition.key]: event.target.value }))}><option value="">Не выбрано</option>{definition.options.map((option) => <option key={option} value={option}>{option}</option>)}</select>
              : <input type={definition.field_type === "number" ? "number" : definition.field_type === "date" ? "date" : "text"} value={String(draft[definition.key] ?? "")} disabled={disabled} onChange={(event) => setDraft((current) => ({ ...current, [definition.key]: event.target.value }))} />}
        </label>)}
        <button type="submit" disabled={saving || disabled}>{saving ? "Сохраняем…" : "Сохранить поля"}</button>
        {error ? <small className="message-error" role="alert">{error}</small> : null}
      </form>
    </div>
  </section>;
}

function NextPurchaseEditor({ deal, disabled, onSave }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetNextPurchase"] }) {
  const [date, setDate] = useState(deal.nextPurchaseAt?.slice(0, 10) ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await onSave(deal.id, date || null);
    } catch {
      setError("Не удалось сохранить дату");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="next-purchase-editor" onSubmit={(event) => void submit(event)}>
      <input aria-label="Дата следующей покупки" type="date" value={date} disabled={disabled} onChange={(event) => setDate(event.target.value)} />
      <button type="submit" disabled={saving || disabled}>{saving ? "…" : "Сохранить"}</button>
      {deal.nextPurchaseAt ? <small>{formatLongDate(deal.nextPurchaseAt)}</small> : null}
      {error ? <small className="message-error" role="alert">{error}</small> : null}
    </form>
  );
}

function DrawerTasks({ deal, onToggleTask, expanded = false }: { deal: Deal; onToggleTask: DealDrawerProps["onToggleTask"]; expanded?: boolean }) {
  return (
    <section className={`drawer-block${expanded ? " drawer-block--expanded" : ""}`}>
      <header><h3>Задачи</h3><span className="drawer-block__count">{deal.tasks.length}</span></header>
      {deal.tasks.length ? deal.tasks.map((task) => (
        <button key={task.id} type="button" className={`task-row${task.completed ? " task-row--done" : ""}`} onClick={() => void onToggleTask(deal.id, task.id)}>
          <span className="task-row__check">{task.completed ? <Check size={14} /> : <Circle size={14} />}</span>
          <span><strong>{task.title}</strong><time>{formatTime(task.dueAt)}</time></span>
          <Avatar user={task.assignee} size="sm" />
        </button>
      )) : <p className="empty-copy">Нет открытых задач</p>}
    </section>
  );
}

function DrawerMessages({ deal, onSubmit, onRetryMessage, error, expanded = false }: { deal: Deal; onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>; onRetryMessage: DealDrawerProps["onRetryMessage"]; error: string; expanded?: boolean }) {
  const [attachmentName, setAttachmentName] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    await onSubmit(event);
    setAttachmentName("");
  }

  return (
    <section className={`drawer-block messages-block${expanded ? " drawer-block--expanded" : ""}`}>
      <header><h3>Переписка</h3><span>{deal.sourceLabel}</span></header>
      <div className="message-list">
        {deal.messages.length ? deal.messages.map((message) => (
          <div key={message.id} className={`message message--${message.direction}`}>
            <p>{message.body}</p>
            <time>
              {formatTime(message.createdAt)}
              {message.status === "sent" ? " ✓✓" : message.status === "queued" ? " · в очереди" : message.status === "failed" ? " · ошибка" : ""}
            </time>
            {message.status === "failed" ? (
              <button
                type="button"
                className="message__retry"
                title={message.lastError}
                onClick={() => void onRetryMessage(deal.id, message.id)}
              >
                Повторить
              </button>
            ) : null}
          </div>
        )) : <p className="empty-copy">Сообщений пока нет</p>}
      </div>
      {error ? <p className="message-error" role="alert">{error}</p> : null}
      {attachmentName ? <span className="message-attachment-chip"><Paperclip size={13} /> {attachmentName}</span> : null}
      <form className="message-composer" onSubmit={(event) => void submit(event)}>
        <label className="message-composer__attachment" aria-label="Прикрепить файл"><Paperclip size={18} /><input name="attachment" type="file" accept=".jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.txt,.csv" onChange={(event) => setAttachmentName(event.target.files?.[0]?.name ?? "")} /></label>
        <input name="message" placeholder="Написать сообщение" autoComplete="off" />
        <button type="submit" aria-label="Отправить"><Send size={18} /></button>
      </form>
    </section>
  );
}
