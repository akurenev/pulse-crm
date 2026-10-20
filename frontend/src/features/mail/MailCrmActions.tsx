import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckSquare2, Plus, UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/Button";
import { api, remoteEnabled } from "../../lib/api";
import { useAuth } from "../../state/auth-store";
import { useCrm } from "../../state/crm-store";
import type { ApiContact, ApiDeal, ApiSource, ApiTask, CursorPage } from "../../types/api";
import type { MailAccount, MailMessage } from "../../types/mail";
import { MailDialog } from "./MailDialogs";
import { mailError } from "./helpers";

type Action = "contact" | "deal" | "task";

/** All CRM writes here are explicit user actions; mailbox reads never create records. */
export function MailCrmActions({ message, account, onChanged }: {
  message: MailMessage; account: MailAccount; onChanged: () => Promise<void>;
}) {
  const { session } = useAuth();
  const { pipeline, pipelines, addDeal } = useCrm();
  const queryClient = useQueryClient();
  const partner = message.direction === "inbound" ? message.from_address : message.to_addresses.find((address) => address.toLowerCase() !== account.email.toLowerCase()) ?? "";
  const contactsQuery = useQuery({
    queryKey: ["mail", "matching-contacts", partner],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(`/contacts?limit=20&search=${encodeURIComponent(partner)}`, { signal }),
    enabled: remoteEnabled && !!partner,
  });
  const contacts = (contactsQuery.data?.items ?? []).filter((contact) => [contact.primary_email, ...contact.emails].some((email) => email?.toLowerCase() === partner.toLowerCase()));
  const [action, setAction] = useState<Action | null>(null);
  const [title, setTitle] = useState("");
  const [selectedPipeline, setSelectedPipeline] = useState(pipeline.id);
  const [selectedContact, setSelectedContact] = useState("");
  const [due, setDue] = useState("");
  const [share, setShare] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [createdDealId, setCreatedDealId] = useState<string | null>(null);
  const [result, setResult] = useState<{ text: string; href?: string } | null>(null);

  function open(next: Action) {
    setAction(next); setError(""); setShare(false); setCreatedDealId(null);
    setSelectedContact(contacts.length === 1 ? contacts[0].id : "");
    const initialTitle = next === "contact" ? (message.direction === "inbound" ? message.from_name : null) || partner.split("@")[0] : message.subject || "Обращение по электронной почте";
    setTitle(initialTitle.slice(0, next === "contact" ? 120 : 240));
    const tomorrow = new Date(Date.now() + 86_400_000);
    setDue(`${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, "0")}-${String(tomorrow.getDate()).padStart(2, "0")}T${String(tomorrow.getHours()).padStart(2, "0")}:${String(tomorrow.getMinutes()).padStart(2, "0")}`);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action || !session) return;
    setPending(true); setError("");
    try {
      if (action === "contact") {
        if (remoteEnabled) {
          const names = title.trim().split(/\s+/);
          const contact = await api.post<ApiContact>("/contacts", { first_name: names[0], last_name: names.slice(1).join(" "), primary_email: partner, assignee_id: session.user.id });
          setResult({ text: "Контакт создан", href: `/contacts?contact=${encodeURIComponent(contact.id)}` });
        } else setResult({ text: "Демонстрационный режим: контакт не сохранён на сервере" });
      } else if (action === "deal") {
        const target = pipelines.find((item) => item.id === selectedPipeline);
        const firstStage = target?.stages.find((stage) => stage.stageType === "open");
        if (!target || !firstStage) throw new Error("В воронке нет рабочего этапа");
        let dealId = createdDealId;
        if (!dealId) {
          if (remoteEnabled) {
            const sources = await api.get<ApiSource[]>("/sources");
            const deal = await api.post<ApiDeal>("/deals", { title: title.trim(), pipeline_id: target.id, stage_id: firstStage.id, assignee_id: session.user.id, source_id: sources.find((source) => source.key === "email")?.id ?? null, contact_ids: selectedContact ? [selectedContact] : [], amount: 0 });
            dealId = deal.id;
          } else {
            const deal = await addDeal({ title: title.trim(), amount: 0, source: "email" });
            dealId = deal.id;
          }
          // A retry after a failed snapshot must reuse the already created deal.
          setCreatedDealId(dealId);
        }
        if (remoteEnabled && share) await api.post(`/mail/messages/${message.id}/link`, { deal_id: dealId });
        setResult({ text: remoteEnabled && share ? "Сделка создана, письмо прикреплено" : "Сделка создана", href: `/deals?deal=${encodeURIComponent(dealId)}` });
      } else {
        if (remoteEnabled) {
          const task = await api.post<ApiTask>("/tasks", { title: title.trim(), description: share ? `Письмо от ${message.from_address}\nТема: ${message.subject}\n\n${message.body_text}` : null, task_type: "follow_up", due_at: new Date(due).toISOString(), assignee_id: session.user.id, contact_id: selectedContact || null });
          setResult({ text: "Задача создана", href: `/tasks?task=${encodeURIComponent(task.id)}` });
        } else setResult({ text: "Демонстрационный режим: задача не сохранена на сервере" });
      }
      await queryClient.invalidateQueries({ queryKey: ["mail"] });
      await onChanged();
      window.dispatchEvent(new Event("pulse:refresh"));
      setAction(null);
    } catch (cause) { setError(cause instanceof Error && cause.message === "В воронке нет рабочего этапа" ? cause.message : mailError(cause)); }
    finally { setPending(false); }
  }

  return <div className="mail-crm-actions">
    {contacts.length ? <p className="mail-muted">Контакт: {contacts.map((contact) => <Link key={contact.id} to={`/contacts?contact=${encodeURIComponent(contact.id)}`}>{`${contact.first_name} ${contact.last_name}`.trim()}</Link>)}</p> : null}
    {contactsQuery.isError ? <p className="mail-muted">Не удалось проверить совпадения контактов. <button type="button" onClick={() => void contactsQuery.refetch()}>Повторить</button></p> : null}
    <div className="mail-reply-actions">
      {partner && !contacts.length ? <Button compact disabled={contactsQuery.isFetching} onClick={() => open("contact")}><UserPlus size={15} />Создать контакт</Button> : null}
      <Button compact onClick={() => open("deal")}><Plus size={15} />Создать сделку</Button>
      <Button compact onClick={() => open("task")}><CheckSquare2 size={15} />Создать задачу</Button>
    </div>
    {result ? <p className="mail-success" role="status">{result.text}{result.href ? <> · <Link to={result.href}>Открыть</Link></> : null}</p> : null}
    {action ? <MailDialog title={action === "contact" ? "Создать контакт из письма" : action === "deal" ? "Создать сделку из письма" : "Создать задачу из письма"} description="Проверьте данные перед сохранением в CRM. Новая запись будет назначена на вас." onClose={() => { if (!pending) setAction(null); }}>
      <form className="mail-form" onSubmit={(event) => void submit(event)}>
        {error ? <p role="alert" className="mail-alert">{error}</p> : null}
        {createdDealId ? <p className="mail-alert">Сделка уже создана. Повторная попытка прикрепит письмо к ней.</p> : null}
        <label>{action === "contact" ? "Имя и фамилия" : "Название"}<input value={title} required maxLength={action === "contact" ? 120 : 240} disabled={pending || !!createdDealId} onChange={(event) => setTitle(event.target.value)} /></label>
        {action === "contact" ? <label>Email<input value={partner} readOnly type="email" /></label> : <>
          {contacts.length ? <label>Контакт<select value={selectedContact} disabled={pending || !!createdDealId} onChange={(event) => setSelectedContact(event.target.value)}><option value="">Без контакта</option>{contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.first_name} {contact.last_name}</option>)}</select></label> : null}
          {action === "deal" ? <label>Воронка<select value={selectedPipeline} disabled={pending || !!createdDealId || !remoteEnabled} onChange={(event) => setSelectedPipeline(event.target.value)}>{pipelines.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label> : <label>Срок<input type="datetime-local" value={due} required onChange={(event) => setDue(event.target.value)} /></label>}
          <label className="mail-consent"><input type="checkbox" checked={share} onChange={(event) => setShare(event.target.checked)} /><span>{action === "deal" ? "Прикрепить копию письма с вложениями. Она будет доступна всем, у кого есть доступ к сделке." : "Включить текст письма в описание задачи. Вложения не копируются."}</span></label>
        </>}
        <div className="dialog-actions"><Button type="button" onClick={() => setAction(null)} disabled={pending}>Отмена</Button><Button type="submit" variant="primary" disabled={pending || !title.trim()}>{pending ? "Сохраняем…" : createdDealId ? "Прикрепить письмо" : "Создать"}</Button></div>
      </form>
    </MailDialog> : null}
  </div>;
}
