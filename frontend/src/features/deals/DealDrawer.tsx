import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
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
import type { ApiActivity, ApiAttachmentDownload, ApiCompany, ApiContact, ApiCustomField, ApiNoteAttachment, CursorPage } from "../../types/api";
import type { Deal, Pipeline, UserSummary } from "../../types/crm";

const MAX_NOTE_FILES = 5;
const MAX_NOTE_FILE_BYTES = 20 * 1024 * 1024;
const NOTE_FILE_ACCEPT = ".jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.txt,.csv";

interface DealDrawerProps {
  deal: Deal | null;
  pipeline: Pipeline;
  assignees: UserSummary[];
  canAccessCompanies?: boolean;
  canDelete?: boolean;
  canManageAssignee?: boolean;
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
  onOpenContact?: (contactId: string) => void;
  onOpenCompany?: (companyId: string) => void;
}

export function DealDrawer({ deal, pipeline, assignees, canAccessCompanies = true, canDelete = true, canManageAssignee = true, mutationPending, onClose, onMove, onSetNextPurchase, onSetContact, onSetCompany, onSetAssignee, onSetTags, onSetCustomFields, onSendMessage, onRetryMessage, onToggleTask, onDelete, onOpenContact, onOpenCompany }: DealDrawerProps) {
  const queryClient = useQueryClient();
  const overlayModal = useMediaQuery("(max-width: 1100px)");
  const [tab, setTab] = useState("details");
  const [messageError, setMessageError] = useState("");
  const [noteError, setNoteError] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteFiles, setNoteFiles] = useState<File[]>([]);
  const [noteFilesInvalid, setNoteFilesInvalid] = useState(false);
  const [downloadingNoteAttachmentId, setDownloadingNoteAttachmentId] = useState<string | null>(null);
  const [noteDownloadError, setNoteDownloadError] = useState("");
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [stageError, setStageError] = useState("");
  const historyQuery = useQuery({
    queryKey: ["activity", "deal", deal?.id],
    queryFn: ({ signal }) => api.get<CursorPage<ApiActivity>>(
      `/activity?limit=100&entity_type=deal&entity_id=${deal?.id}`,
      { signal },
    ),
    enabled: remoteEnabled && Boolean(deal?.id),
  });
  useEffect(() => {
    setDeleteConfirmOpen(false);
    setDeleteError("");
    setStageError("");
    setTab("details");
    setNoteFiles([]);
    setNoteFilesInvalid(false);
    setNoteError("");
    setNoteDownloadError("");
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
      if (noteFiles.length) {
        const upload = new FormData();
        upload.set("body", body);
        for (const file of noteFiles) upload.append("files", file);
        await api.upload<ApiActivity>(`/deals/${deal.id}/notes/with-attachments`, upload);
      } else {
        await api.post<ApiActivity>(`/deals/${deal.id}/notes`, { body });
      }
      form.reset();
      setNoteFiles([]);
      await queryClient.invalidateQueries({ queryKey: ["activity", "deal", deal.id] });
    } catch (reason) {
      setNoteError(noteMutationErrorMessage(reason));
    } finally {
      setNoteSaving(false);
    }
  }

  function selectNoteFiles(files: FileList | null) {
    const next = Array.from(files ?? []);
    if (next.length > MAX_NOTE_FILES) {
      setNoteFiles([]);
      setNoteFilesInvalid(true);
      setNoteError(`К одной заметке можно прикрепить не больше ${MAX_NOTE_FILES} файлов.`);
      return;
    }
    const oversized = next.find((file) => file.size > MAX_NOTE_FILE_BYTES);
    if (oversized) {
      setNoteFiles([]);
      setNoteFilesInvalid(true);
      setNoteError(`Файл «${oversized.name}» превышает лимит 20 МБ.`);
      return;
    }
    setNoteFilesInvalid(false);
    setNoteError("");
    setNoteFiles(next);
  }

  async function downloadNoteAttachment(attachment: ApiNoteAttachment) {
    setDownloadingNoteAttachmentId(attachment.id);
    setNoteDownloadError("");
    try {
      const download = await api.get<ApiAttachmentDownload>(`/note-attachments/${attachment.id}/download`);
      const link = document.createElement("a");
      link.href = download.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.click();
    } catch {
      setNoteDownloadError("Не удалось открыть файл. Обновите карточку и повторите.");
    } finally {
      setDownloadingNoteAttachmentId(null);
    }
  }

  async function handleDelete() {
    if (!deal) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await onDelete(deal.id);
      setDeleteConfirmOpen(false);
      onClose();
    } catch (reason) {
      setDeleteError(dealMutationErrorMessage(reason, "Не удалось удалить сделку. Повторите попытку."));
    } finally {
      setDeleting(false);
    }
  }

  const activityItems = historyQuery.data?.items ?? [];
  const noteItems = activityItems.filter((event) => event.event_type === "deal.note.created");
  const historyItems = activityItems.filter((event) => event.event_type !== "deal.note.created");

  return (
    <>
      <Dialog.Root open modal={overlayModal && !(canDelete && deleteConfirmOpen)} onOpenChange={(open) => { if (!open && !(canDelete && deleteConfirmOpen)) onClose(); }}>
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
              {canDelete ? <button
                type="button"
                className="icon-button deal-drawer__delete"
                aria-label="Удалить сделку"
                title="Удалить сделку"
                disabled={mutationPending}
                onClick={() => { setDeleteError(""); setDeleteConfirmOpen(true); }}
              >
                <Trash2 size={18} aria-hidden="true" />
              </button> : null}
              <Dialog.Close className="icon-button deal-drawer__close" aria-label="Закрыть карточку">
                <X size={19} />
              </Dialog.Close>
            </div>
          </header>
          {stageError ? <p className="form-error" role="alert">{stageError}</p> : null}

          <Tabs.Root value={tab} onValueChange={setTab} className="deal-tabs">
            <Tabs.List aria-label="Разделы сделки">
              <Tabs.Trigger value="details">Детали</Tabs.Trigger>
              <Tabs.Trigger value="fields">Поля</Tabs.Trigger>
              <Tabs.Trigger value="tasks">Задачи</Tabs.Trigger>
              <Tabs.Trigger value="messages">Переписка{deal.messages.length ? <span>{deal.messages.length}</span> : null}</Tabs.Trigger>
              <Tabs.Trigger value="history">История</Tabs.Trigger>
            </Tabs.List>

            <div className="deal-drawer__scroll">
              <Tabs.Content value="details" className="drawer-section">
                <dl className="details-list deal-details">
                  <div className="deal-details__row deal-details__row--contact">
                    <dt className="deal-details__label"><UserRound size={17} aria-hidden="true" /><span className="deal-details__label-text">Контакт</span></dt>
                    <dd className="deal-details__value"><ContactPicker deal={deal} disabled={mutationPending} onSave={onSetContact} onOpen={onOpenContact} /></dd>
                  </div>
                  {deal.phone ? <div className="deal-details__row deal-details__row--phone"><dt className="deal-details__label"><Phone size={17} aria-hidden="true" /><span className="deal-details__label-text">Телефон</span></dt><dd className="deal-details__value"><a href={`tel:${deal.phone}`}>{deal.phone}</a></dd></div> : null}
                  {deal.email ? <div className="deal-details__row deal-details__row--email"><dt className="deal-details__label"><Mail size={17} aria-hidden="true" /><span className="deal-details__label-text">Email</span></dt><dd className="deal-details__value"><a href={`mailto:${deal.email}`}>{deal.email}</a></dd></div> : null}
                  {canAccessCompanies ? <div className="deal-details__row deal-details__row--company">
                    <dt className="deal-details__label"><Building2 size={17} aria-hidden="true" /><span className="deal-details__label-text">Компания</span></dt>
                    <dd className="deal-details__value"><CompanyPicker deal={deal} disabled={mutationPending} onSave={onSetCompany} onOpen={onOpenCompany} /></dd>
                  </div> : null}
                  <div className="deal-details__row deal-details__row--owner">
                    <dt className="deal-details__label"><UserRoundCheck size={17} aria-hidden="true" /><span className="deal-details__label-text">Ответственный</span></dt>
                    <dd className="deal-details__value">{canManageAssignee
                      ? <AssigneePicker key={deal.id} deal={deal} assignees={assignees} disabled={mutationPending} onSave={onSetAssignee} />
                      : <span className="relation-value owner-value"><span className="owner-line"><Avatar user={deal.assignee} size="sm" /><span>{deal.assignee.name}</span></span></span>}</dd>
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
                <form key={deal.id} className="note-composer note-composer--details" onSubmit={(event) => void handleNote(event)}>
                  <label htmlFor={`deal-note-${deal.id}`}>Заметка о сделке</label>
                  <textarea id={`deal-note-${deal.id}`} name="note" rows={3} maxLength={10_000} required placeholder="Зафиксировать договорённость или итог разговора" />
                  <div className="note-composer__file-row">
                    <label className="note-file-picker">
                      <Paperclip size={15} aria-hidden="true" />
                      <span>Прикрепить файлы</span>
                      <input
                        type="file"
                        multiple
                        accept={NOTE_FILE_ACCEPT}
                        aria-label="Добавить файлы к заметке"
                        disabled={noteSaving}
                        onChange={(event) => selectNoteFiles(event.target.files)}
                      />
                    </label>
                    <small>До 5 файлов, каждый до 20 МБ</small>
                  </div>
                  {noteFiles.length ? <ul className="note-file-selection" aria-label="Файлы заметки">
                    {noteFiles.map((file, index) => <li key={`${file.name}:${file.size}:${index}`}>
                      <Paperclip size={13} aria-hidden="true" />
                      <span title={file.name}>{file.name}</span>
                      <small>{formatFileSize(file.size)}</small>
                      <button type="button" aria-label={`Убрать файл ${file.name}`} disabled={noteSaving} onClick={() => setNoteFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))}><X size={13} aria-hidden="true" /></button>
                    </li>)}
                  </ul> : null}
                  <p className="note-composer__privacy">Вложения сохраняются в заметке CRM и не отправляются клиенту в переписке.</p>
                  <button type="submit" disabled={noteSaving || noteFilesInvalid}>{noteSaving ? "Сохраняем…" : "Добавить заметку"}</button>
                  {noteError ? <small className="message-error" role="alert">{noteError}</small> : null}
                </form>
                <section className="deal-notes" aria-labelledby={`deal-notes-title-${deal.id}`}>
                  <header><h3 id={`deal-notes-title-${deal.id}`}>Заметки</h3><small>{historyQuery.isError ? "Ошибка загрузки" : noteItems.length || "Нет заметок"}</small></header>
                  {historyQuery.isLoading ? <p className="empty-copy">Загружаем заметки…</p> : null}
                  {historyQuery.isError ? <div className="drawer-query-error" role="alert"><p>Не удалось загрузить заметки.</p><button type="button" onClick={() => void historyQuery.refetch()}>Повторить</button></div> : null}
                  {!historyQuery.isError ? <ol className="deal-notes__list">
                    {noteItems.map((event) => <li key={event.id}>
                      <p>{activityDetail(event)}</p>
                      {(event.attachments ?? []).length ? <ul className="note-attachments" aria-label="Вложения заметки">
                        {(event.attachments ?? []).map((attachment) => <li key={attachment.id}><button type="button" disabled={downloadingNoteAttachmentId === attachment.id} onClick={() => void downloadNoteAttachment(attachment)}><Paperclip size={14} aria-hidden="true" /><span title={attachment.original_filename}>{attachment.original_filename}</span><small>{formatFileSize(attachment.size_bytes)}</small></button></li>)}
                      </ul> : null}
                      <time>{formatActivityDate(event.occurred_at)}</time>
                    </li>)}
                  </ol> : null}
                  {!historyQuery.isLoading && !historyQuery.isError && !noteItems.length ? <p className="empty-copy">Договорённости и приложенные документы появятся здесь.</p> : null}
                  {noteDownloadError ? <p className="message-error" role="alert">{noteDownloadError}</p> : null}
                </section>
              </Tabs.Content>

              <Tabs.Content value="fields" className="drawer-section drawer-section--fields">
                <CustomFieldsEditor key={deal.id} deal={deal} disabled={mutationPending} onSave={onSetCustomFields} />
              </Tabs.Content>

              <Tabs.Content value="tasks" className="drawer-section">
                <DrawerTasks deal={deal} onToggleTask={onToggleTask} expanded />
              </Tabs.Content>

              <Tabs.Content value="messages" className="drawer-section drawer-section--messages">
                <DrawerMessages deal={deal} onSubmit={handleMessage} onRetryMessage={onRetryMessage} error={messageError} expanded />
              </Tabs.Content>

              <Tabs.Content value="history" className="drawer-section">
                {historyQuery.isLoading ? <p className="empty-copy timeline-loading">Загружаем историю…</p> : null}
                {historyQuery.isError ? <div className="drawer-query-error drawer-query-error--timeline" role="alert"><p>Не удалось загрузить историю сделки.</p><button type="button" onClick={() => void historyQuery.refetch()}>Повторить</button></div> : null}
                {!historyQuery.isError ? <ol className="timeline">
                  {remoteEnabled ? historyItems.map((event) => (
                    <li key={event.id}><span /><div><strong>{activityTitle(event.event_type)}</strong><p>{activityDetail(event)}</p><time>{formatActivityDate(event.occurred_at)}</time></div></li>
                  )) : <>
                    <li><span /><div><strong>Сделка создана</strong><p>Источник: {deal.sourceLabel}</p><time>Сегодня</time></div></li>
                    <li><span /><div><strong>Назначен ответственный</strong><p>{deal.assignee.name}</p><time>Сегодня</time></div></li>
                    <li><span /><div><strong>Этап изменён</strong><p>{pipeline.stages.find((stage) => stage.id === deal.stageId)?.name}</p><time>Сегодня</time></div></li>
                  </>}
                </ol> : null}
                {remoteEnabled && !historyQuery.isLoading && !historyQuery.isError && !historyItems.length ? <p className="empty-copy">История пока пуста</p> : null}
              </Tabs.Content>
            </div>
          </Tabs.Root>
        </Dialog.Content>
      </Dialog.Portal>
      </Dialog.Root>

      {canDelete ? <Dialog.Root
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
      </Dialog.Root> : null}
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

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} КБ`;
  return `${(bytes / (1024 * 1024)).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;
}

function noteMutationErrorMessage(reason: unknown) {
  if (!(reason instanceof ApiError)) return "Не удалось сохранить заметку. Повторите попытку.";
  if (reason.status === 404) return "Сделка не найдена или больше вам недоступна.";
  if (reason.status === 413) return "Один из файлов превышает лимит 20 МБ.";
  if (reason.status === 503) return "Хранилище файлов временно недоступно. Попробуйте позже.";
  const detail = reason.details && typeof reason.details === "object" && "detail" in reason.details
    ? (reason.details as { detail?: unknown }).detail
    : undefined;
  if (detail === "archives and executable files are not allowed") return "Архивы и исполняемые файлы прикреплять нельзя.";
  if (detail === "file extension and content type are not allowed") return "Этот тип файла нельзя прикрепить к заметке.";
  if (detail === "attachment must not be empty") return "Нельзя прикрепить пустой файл.";
  return "Не удалось сохранить заметку. Проверьте файлы и повторите попытку.";
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

function ContactPicker({ deal, disabled, onSave, onOpen }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetContact"]; onOpen?: DealDrawerProps["onOpenContact"] }) {
  const [editing, setEditing] = useState(false);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const results = useQuery({
    queryKey: ["contact-picker", deferredSearch],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(
      `/contacts?limit=20&search=${encodeURIComponent(deferredSearch)}`,
      { signal },
    ),
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
    const contactId = deal.contactIds?.[0];
    const contactName = deal.contactName ?? "Не указан";
    return <span className="relation-value">
      {contactId && onOpen
        ? <button type="button" className="deal-relation-link" aria-label={`Открыть контакт ${contactName}`} onClick={() => onOpen(contactId)}><span>{contactName}</span><ArrowUpRight size={14} aria-hidden="true" /></button>
        : <span>{contactName}</span>}
      {remoteEnabled ? <EditFieldButton label="Изменить контакт сделки" disabled={disabled} onClick={() => setEditing(true)} /> : null}
    </span>;
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

function CompanyPicker({ deal, disabled, onSave, onOpen }: { deal: Deal; disabled: boolean; onSave: DealDrawerProps["onSetCompany"]; onOpen?: DealDrawerProps["onOpenCompany"] }) {
  const [editing, setEditing] = useState(false);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const results = useQuery({
    queryKey: ["company-picker", deferredSearch],
    queryFn: ({ signal }) => api.get<CursorPage<ApiCompany>>(
      `/companies?limit=20&search=${encodeURIComponent(deferredSearch)}`,
      { signal },
    ),
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
    const companyId = deal.companyId;
    const companyName = deal.companyName ?? "Не указана";
    return <span className="relation-value">
      {companyId && onOpen
        ? <button type="button" className="deal-relation-link" aria-label={`Открыть компанию ${companyName}`} onClick={() => onOpen(companyId)}><span>{companyName}</span><ArrowUpRight size={14} aria-hidden="true" /></button>
        : <span>{companyName}</span>}
      {remoteEnabled ? <EditFieldButton label="Изменить компанию сделки" disabled={disabled} onClick={() => setEditing(true)} /> : null}
    </span>;
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
    queryFn: ({ signal }) => api.get<ApiCustomField[]>("/custom-fields?entity_type=deal", { signal }),
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
