import * as Dialog from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Paperclip, X } from "lucide-react";
import { useDeferredValue, useState, type FormEvent, type ReactNode } from "react";

import { Button } from "../../components/Button";
import { api, ApiError, remoteEnabled } from "../../lib/api";
import { useCrm } from "../../state/crm-store";
import type { ApiDeal, CursorPage } from "../../types/api";
import type { MailAccount, MailAccountInput, MailAttachment, MailCompose, MailMessage } from "../../types/mail";
import { fileSize, mailError, parseAddresses, validAddresses } from "./helpers";

export function MailDialog({ title, description, onClose, children, wide = false }: { title: string; description: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  return <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
    <Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className={`dialog-content mail-dialog${wide ? " mail-dialog--wide" : ""}`} onPointerDownOutside={(event) => event.preventDefault()}>
      <header className="dialog-header"><div><Dialog.Title>{title}</Dialog.Title><Dialog.Description>{description}</Dialog.Description></div><button className="icon-button" aria-label="Закрыть" type="button" onClick={onClose}><X size={20} /></button></header>
      {children}
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>;
}

const defaultAccount: MailAccountInput = {
  email: "", display_name: "Рабочая почта", username: "", password: "", imap_host: "imap.majordomo.ru", imap_port: 993,
  imap_security: "ssl", smtp_host: "smtp.majordomo.ru", smtp_port: 587, smtp_security: "starttls", sync_days: 30,
};

export function MailAccountEditor({ account, onClose, onSaved, onDisconnect, onConflict }: {
  account?: MailAccount; onClose: () => void; onSaved: (input: MailAccountInput) => Promise<void>;
  onDisconnect?: () => Promise<void>; onConflict: () => Promise<void>;
}) {
  const [form, setForm] = useState<MailAccountInput>(() => account ? { ...account, password: "" } : { ...defaultAccount });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const update = <K extends keyof MailAccountInput>(key: K, value: MailAccountInput[K]) => setForm((old) => ({ ...old, [key]: value }));
  async function submit(event: FormEvent) {
    event.preventDefault(); setPending(true); setError("");
    try { await onSaved({ ...form, username: form.username || form.email }); }
    catch (cause) { setError(mailError(cause)); if (cause instanceof ApiError && cause.status === 409) await onConflict(); }
    finally { setPending(false); }
  }
  return <MailDialog title={account ? "Настройки моего ящика" : "Подключить рабочую почту"} description="Ящик доступен только вам. Письма попадают в сделку после вашего подтверждения." onClose={() => { if (!pending) onClose(); }}>
    <form className="mail-form" onSubmit={(event) => void submit(event)}>
      {error ? <p className="mail-alert" role="alert">{error}</p> : null}
      <label>Название<input value={form.display_name} maxLength={160} required onChange={(event) => update("display_name", event.target.value)} /></label>
      <label>Адрес почты<input type="email" placeholder="employee@example.com" value={form.email} required disabled={!!account} autoComplete="off" onChange={(event) => update("email", event.target.value)} /></label>
      <label>{account?.status === "disabled" ? "Пароль для повторного подключения" : account ? "Новый пароль (пусто — оставить текущий)" : "Пароль ящика"}<input type="password" value={form.password} required={!account || (remoteEnabled && !account.has_credentials)} autoComplete="new-password" onChange={(event) => update("password", event.target.value)} /></label>
      <label>Загрузить историю за дни<input type="number" min={1} max={365} required value={form.sync_days} onChange={(event) => update("sync_days", Number(event.target.value))} /></label>
      <details className="mail-server-settings"><summary>Серверы IMAP и SMTP · Majordomo</summary>
        <p>Предустановлены параметры Majordomo. Используется зашифрованное соединение.</p>
        <label>Логин (по умолчанию адрес почты)<input value={form.username} disabled={!!account} autoComplete="off" onChange={(event) => update("username", event.target.value)} /></label>
        <div className="mail-form__row"><label>Сервер IMAP<input value={form.imap_host} disabled={!!account} required onChange={(event) => update("imap_host", event.target.value)} /></label><label>Порт IMAP<input type="number" min={1} max={65535} value={form.imap_port} disabled={!!account} required onChange={(event) => update("imap_port", Number(event.target.value))} /></label></div>
        <label>Защита IMAP<select value={form.imap_security} disabled={!!account} onChange={(event) => update("imap_security", event.target.value as "ssl" | "starttls")}><option value="ssl">SSL/TLS</option><option value="starttls">STARTTLS</option></select></label>
        <div className="mail-form__row"><label>Сервер SMTP<input value={form.smtp_host} disabled={!!account} required onChange={(event) => update("smtp_host", event.target.value)} /></label><label>Порт SMTP<input type="number" min={1} max={65535} value={form.smtp_port} disabled={!!account} required onChange={(event) => update("smtp_port", Number(event.target.value))} /></label></div>
        <label>Защита SMTP<select value={form.smtp_security} disabled={!!account} onChange={(event) => update("smtp_security", event.target.value as "ssl" | "starttls")}><option value="starttls">STARTTLS</option><option value="ssl">SSL/TLS</option></select></label>
        {account ? <p>Для смены серверов отключите и заново подключите ящик.</p> : null}
      </details>
      {!remoteEnabled ? <p className="mail-muted">Демонстрационный режим: подключение к серверам и отправка писем не выполняются. Не вводите настоящий пароль.</p> : null}
      <div className="dialog-actions"><Button type="button" onClick={onClose} disabled={pending}>Отмена</Button><Button variant="primary" type="submit" disabled={pending}>{pending ? "Сохраняем…" : account?.status === "disabled" ? "Подключить повторно" : account ? "Сохранить настройки" : "Подключить ящик"}</Button></div>
    </form>
    {account && account.status !== "disabled" && onDisconnect ? <div className="mail-disconnect">
      {confirmDisconnect ? <><p>Отключить ящик? Синхронизация остановится, доступ к письмам в почте закроется. Копии в сделках сохранятся.</p><Button variant="danger" disabled={pending} onClick={() => { setPending(true); void onDisconnect().catch((cause: unknown) => { setError(mailError(cause)); if (cause instanceof ApiError && cause.status === 409) void onConflict(); }).finally(() => setPending(false)); }}>Да, отключить ящик</Button><Button variant="ghost" onClick={() => setConfirmDisconnect(false)} disabled={pending}>Оставить</Button></> : <Button variant="ghost" onClick={() => setConfirmDisconnect(true)}>Отключить ящик</Button>}
    </div> : null}
  </MailDialog>;
}

export function MailComposer({ account, initial, draft, forward, forwardedAttachments = [], onClose, onSave, onConflict, onRemoveAttachment }: {
  account: MailAccount; initial: MailCompose; draft?: MailMessage; forward?: boolean; forwardedAttachments?: MailAttachment[]; onClose: () => void;
  onSave: (compose: MailCompose, files: File[], send: boolean, draft: MailMessage | undefined, progress: (message: MailMessage, remainingFiles: File[]) => void) => Promise<MailMessage>;
  onConflict: (id: string) => Promise<MailMessage>;
  onRemoveAttachment: (message: MailMessage, attachment: MailAttachment) => Promise<MailMessage>;
}) {
  const [to, setTo] = useState(initial.to_addresses.join(", "));
  const [cc, setCc] = useState(initial.cc_addresses.join(", "));
  const [bcc, setBcc] = useState(initial.bcc_addresses.join(", "));
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body_text);
  const [signature, setSignature] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [saved, setSaved] = useState(draft);
  const [pending, setPending] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [closeConfirm, setCloseConfirm] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const change = (fn: () => void) => { fn(); setDirty(true); setNotice(""); };
  const close = () => { if (pending) return; if (dirty || files.length) setCloseConfirm(true); else onClose(); };
  async function save(send: boolean) {
    const compose: MailCompose = { ...initial, subject, body_text: body, to_addresses: parseAddresses(to), cc_addresses: parseAddresses(cc), bcc_addresses: parseAddresses(bcc) };
    if (send && !compose.to_addresses.length) { setError("Укажите хотя бы одного получателя."); return; }
    if (!validAddresses([...compose.to_addresses, ...compose.cc_addresses, ...compose.bcc_addresses])) { setError("Укажите адреса в формате name@example.com, разделяя их запятой."); return; }
    setPending(true); setError(""); setNotice("");
    try {
      const result = await onSave(compose, files, send, saved, (message, remainingFiles) => { setSaved(message); setFiles(remainingFiles); });
      setSaved(result); setFiles([]); setDirty(false);
      if (send) onClose(); else setNotice("Черновик сохранён");
    } catch (cause) {
      setError(mailError(cause));
      if (cause instanceof ApiError && cause.status === 409 && saved) {
        try { setSaved(await onConflict(saved.id)); } catch { /* Keep typed text for manual retry. */ }
      }
    } finally { setPending(false); }
  }
  return <MailDialog title={draft ? "Редактировать черновик" : "Новое письмо"} description={`От: ${account.display_name} <${account.email}>`} onClose={close} wide>
    <form className="mail-form" onSubmit={(event) => { event.preventDefault(); void save(true); }}>
      {error ? <p className="mail-alert" role="alert">{error}</p> : null}
      {notice ? <p className="mail-success" role="status"><CheckCircle2 size={16} />{notice}</p> : null}
      {forward && !saved && forwardedAttachments.length ? <p className="mail-muted">Вложения исходного письма ({forwardedAttachments.length}) будут добавлены к черновику при сохранении. После сохранения их можно удалить.</p> : null}
      <label>Кому<input value={to} placeholder="client@example.com" onChange={(event) => change(() => setTo(event.target.value))} /></label>
      <div className="mail-form__row"><label>Копия<input value={cc} onChange={(event) => change(() => setCc(event.target.value))} /></label><label>Скрытая копия<input value={bcc} onChange={(event) => change(() => setBcc(event.target.value))} /></label></div>
      <label>Тема<input value={subject} maxLength={998} onChange={(event) => change(() => setSubject(event.target.value))} /></label>
      <label>Текст письма<textarea rows={11} value={body} maxLength={100000} onChange={(event) => change(() => setBody(event.target.value))} /></label>
      <details><summary>Подпись</summary><label>Текст подписи<textarea rows={2} value={signature} onChange={(event) => setSignature(event.target.value)} /></label><Button type="button" compact disabled={!signature.trim()} onClick={() => change(() => { setBody((old) => `${old}\n\n${signature.trim()}`); setSignature(""); })}>Добавить подпись в письмо</Button></details>
      {saved?.attachments.map((attachment) => <span key={attachment.id} className="mail-attachment"><Paperclip size={14} /><span>{attachment.original_filename} · {fileSize(attachment.size_bytes)}</span><button className="icon-button" type="button" disabled={pending} aria-label={`Удалить вложение ${attachment.original_filename}`} onClick={() => { setPending(true); setError(""); void onRemoveAttachment(saved, attachment).then(setSaved).catch((cause: unknown) => { setError(mailError(cause)); if (cause instanceof ApiError && cause.status === 409) void onConflict(saved.id).then(setSaved).catch(() => undefined); }).finally(() => setPending(false)); }}><X size={15} /></button></span>)}
      {files.map((file, index) => <div key={`${file.name}-${index}`} className="mail-attachment"><Paperclip size={14} /><span>{file.name} · {fileSize(file.size)}</span><button type="button" className="icon-button" aria-label={`Убрать ${file.name}`} onClick={() => change(() => setFiles((current) => current.filter((_, item) => item !== index)))}><X size={15} /></button></div>)}
      <label className="mail-file-label"><Paperclip size={17} />Прикрепить файлы<input aria-label="Прикрепить файлы" type="file" multiple onChange={(event) => { const selected = Array.from(event.target.files ?? []); change(() => setFiles((old) => [...old, ...selected])); event.target.value = ""; }} /></label>
      <p className="mail-muted">Черновик сохраняется по кнопке. Текст не записывается в хранилище браузера.</p>
      <div className="dialog-actions"><Button type="button" onClick={close} disabled={pending}>Закрыть</Button><Button type="button" onClick={() => void save(false)} disabled={pending}>Сохранить черновик</Button><Button variant="primary" type="submit" disabled={pending || account.status === "disabled"}>{pending ? "Сохраняем…" : "Отправить"}</Button></div>
      {closeConfirm ? <div className="mail-alert"><p>Закрыть редактор и потерять несохранённые изменения?</p><Button type="button" onClick={() => setCloseConfirm(false)}>Продолжить редактирование</Button><Button type="button" variant="danger" onClick={onClose}>Закрыть без сохранения</Button></div> : null}
    </form>
  </MailDialog>;
}

export function MailLinkDialog({ message, onClose, onLink }: { message: MailMessage; onClose: () => void; onLink: (dealId: string) => Promise<void> }) {
  const { deals } = useCrm();
  const [search, setSearch] = useState("");
  const query = useDeferredValue(search.trim());
  const [selected, setSelected] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const dealsQuery = useQuery({ queryKey: ["mail-link-deals", query], queryFn: () => api.get<CursorPage<ApiDeal>>(`/deals?limit=50&search=${encodeURIComponent(query)}`), enabled: remoteEnabled });
  const available = remoteEnabled ? dealsQuery.data?.items ?? [] : deals.filter((deal) => deal.title.toLocaleLowerCase("ru").includes(query.toLocaleLowerCase("ru")));
  return <MailDialog title="Прикрепить письмо к сделке" description="В сделку будет добавлена отдельная копия выбранного письма и его вложений." onClose={() => { if (!pending) onClose(); }}>
    <form className="mail-form" onSubmit={(event) => { event.preventDefault(); setPending(true); setError(""); void onLink(selected).catch((cause: unknown) => setError(mailError(cause))).finally(() => setPending(false)); }}>
      <p className="mail-link-subject">{message.subject || "Без темы"}</p>
      <label>Найти сделку<input type="search" value={search} onChange={(event) => { setSearch(event.target.value); setSelected(""); }} placeholder="Название сделки" /></label>
      <label>Сделка<select value={selected} required onChange={(event) => setSelected(event.target.value)}><option value="">Выберите сделку</option>{available.map((deal) => <option value={deal.id} key={deal.id}>{deal.title}</option>)}</select></label>
      {dealsQuery.isError ? <p role="alert" className="mail-alert">Не удалось загрузить сделки. <button type="button" onClick={() => void dealsQuery.refetch()}>Повторить</button></p> : null}
      {!available.length && !dealsQuery.isFetching ? <p className="mail-muted">Доступные сделки не найдены. Уточните поиск.</p> : null}
      <label className="mail-consent"><input type="checkbox" checked={agreed} onChange={(event) => setAgreed(event.target.checked)} /><span>Разрешаю показать это письмо и вложения всем сотрудникам, у которых есть доступ к выбранной сделке.</span></label>
      {error ? <p role="alert" className="mail-alert">{error}</p> : null}
      <div className="dialog-actions"><Button type="button" onClick={onClose} disabled={pending}>Отмена</Button><Button variant="primary" type="submit" disabled={!selected || !agreed || pending}>{pending ? "Прикрепляем…" : "Прикрепить письмо"}</Button></div>
    </form>
  </MailDialog>;
}
