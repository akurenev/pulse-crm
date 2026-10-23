import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, CheckCheck, ChevronDown, Download, FileText, Forward, Inbox, Link2, Mail, MailOpen, Paperclip, Pencil, Plus, RefreshCw, Reply, ReplyAll, Search, Send, Settings2, ShieldCheck, Star, Trash2 } from "lucide-react";
import { useDeferredValue, useState } from "react";
import { Link } from "react-router-dom";

import { Button } from "../components/Button";
import { demoDraft, demoMailAccount, demoMailFolders, demoMailMessages } from "../features/mail/demo";
import { composeFromMessage, downloadMailAttachment, fileSize, mailError, mailStatusLabels, messageDate } from "../features/mail/helpers";
import { MailCrmActions } from "../features/mail/MailCrmActions";
import { MailAccountEditor, MailComposer, MailLinkDialog } from "../features/mail/MailDialogs";
import { api, ApiError, remoteEnabled } from "../lib/api";
import type { CursorPage } from "../types/api";
import type { MailAccount, MailAccountInput, MailAttachment, MailCompose, MailFolder, MailMessage } from "../types/mail";

const blankCompose: MailCompose = { subject: "", to_addresses: [], cc_addresses: [], bcc_addresses: [], body_text: "" };
const folderIcons = { inbox: Inbox, sent: Send, outbox: Send, drafts: FileText, trash: Trash2, archive: Archive, spam: ShieldCheck, custom: Mail };
type ComposerState = { initial: MailCompose; draft?: MailMessage; forward?: boolean; forwardedAttachments?: MailAttachment[] };

export default function MailPage() {
  const queryClient = useQueryClient();
  const [demoAccounts, setDemoAccounts] = useState<MailAccount[]>([demoMailAccount]);
  const [demoMessages, setDemoMessages] = useState(demoMailMessages);
  const [accountSelection, setAccountSelection] = useState("");
  const [folderSelection, setFolderSelection] = useState("inbox");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim());
  const [unread, setUnread] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [accountEditor, setAccountEditor] = useState<"new" | "edit" | null>(null);
  const [composer, setComposer] = useState<ComposerState | null>(null);
  const [linking, setLinking] = useState(false);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const accountsQuery = useQuery({ queryKey: ["mail", "accounts"], queryFn: () => api.get<{ items: MailAccount[] }>("/mail/accounts"), enabled: remoteEnabled, refetchInterval: 30_000 });
  const accounts = remoteEnabled ? accountsQuery.data?.items ?? [] : demoAccounts;
  const account = accounts.find((item) => item.id === accountSelection) ?? accounts[0];
  const accountId = account?.id;
  const foldersQuery = useQuery({ queryKey: ["mail", "folders", accountId], queryFn: () => api.get<{ items: MailFolder[] }>(`/mail/accounts/${accountId}/folders`), enabled: remoteEnabled && !!accountId, refetchInterval: 15_000 });
  const folders = remoteEnabled ? foldersQuery.data?.items ?? [] : demoMailFolders.map((folder) => ({ ...folder, account_id: accountId ?? "demo-mail", unread_count: demoMessages.filter((item) => item.folder_id === folder.id && !item.is_read).length, total_count: demoMessages.filter((item) => item.folder_id === folder.id).length }));
  const folderId = folderSelection === "inbox" ? folders.find((folder) => folder.kind === "inbox")?.id : folderSelection || undefined;
  const messagesQuery = useInfiniteQuery({
    queryKey: ["mail", "messages", accountId, folderId, deferredSearch, unread],
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ limit: "50" });
      if (folderId) params.set("folder_id", folderId);
      if (deferredSearch) params.set("search", deferredSearch);
      if (unread) params.set("unread", "true");
      if (pageParam) params.set("cursor", pageParam);
      return api.get<CursorPage<MailMessage>>(`/mail/accounts/${accountId}/messages?${params}`, { signal });
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: remoteEnabled && !!accountId && foldersQuery.isSuccess,
    refetchInterval: 15_000,
  });
  const messages = remoteEnabled ? messagesQuery.data?.pages.flatMap((page) => page.items) ?? [] : demoMessages.filter((item) => item.account_id === accountId && (!folderId || item.folder_id === folderId) && (!unread || !item.is_read) && `${item.subject} ${item.from_address} ${item.body_text}`.toLocaleLowerCase("ru").includes(deferredSearch.toLocaleLowerCase("ru")));
  const detailQuery = useQuery({ queryKey: ["mail", "message", selectedId], queryFn: () => api.get<MailMessage>(`/mail/messages/${selectedId}`), enabled: remoteEnabled && !!selectedId, refetchInterval: 15_000 });
  const selected = remoteEnabled ? detailQuery.data : demoMessages.find((item) => item.id === selectedId);
  const threadQuery = useInfiniteQuery({
    queryKey: ["mail", "thread", accountId, selected?.thread_key],
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ thread_key: selected?.thread_key ?? "", limit: "50" });
      if (pageParam) params.set("cursor", pageParam);
      return api.get<CursorPage<MailMessage>>(`/mail/accounts/${accountId}/messages?${params}`, { signal });
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: remoteEnabled && !!selected?.thread_key && ["received", "sent"].includes(selected.status),
    refetchInterval: 15_000,
  });
  const threadMessages = remoteEnabled ? threadQuery.data?.pages.flatMap((page) => page.items) ?? [] : demoMessages.filter((item) => item.account_id === accountId && item.thread_key === selected?.thread_key);


  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["mail"] });
  }
  function storeMessage(message: MailMessage) {
    if (remoteEnabled) queryClient.setQueryData(["mail", "message", message.id], message);
    else setDemoMessages((current) => [message, ...current.filter((item) => item.id !== message.id)]);
  }
  async function run(action: () => Promise<void>) {
    setPending(true); setError(""); setNotice("");
    try { await action(); }
    catch (cause) { setError(mailError(cause)); if (cause instanceof ApiError && cause.status === 409) await refresh(); }
    finally { setPending(false); }
  }
  async function patchMessage(message: MailMessage, patch: Partial<Pick<MailMessage, "is_read" | "is_starred" | "folder_id">>) {
    const result = remoteEnabled ? await api.patch<MailMessage>(`/mail/messages/${message.id}`, { ...patch, expected_version: message.version }) : { ...message, ...patch, version: message.version + 1 };
    storeMessage(result); await refresh();
  }
  async function openMessage(message: MailMessage) {
    setSelectedId(message.id); setError("");
    if (!message.is_read && message.status !== "draft") await run(() => patchMessage(message, { is_read: true }));
  }
  function changeFolder(id: string) { setFolderSelection(id); setSelectedId(null); setError(""); }
  async function saveAccount(input: MailAccountInput) {
    if (!remoteEnabled) {
      const result = { ...demoMailAccount, ...input, id: accountEditor === "edit" ? account!.id : crypto.randomUUID(), has_credentials: false, version: (account?.version ?? 0) + 1 };
      const { password: _password, ...safe } = result; void _password;
      setDemoAccounts((current) => [safe, ...current.filter((item) => item.id !== safe.id)]); setAccountSelection(safe.id);
    } else if (accountEditor === "edit" && account) {
      await api.patch(`/mail/accounts/${account.id}`, { expected_version: account.version, display_name: input.display_name, sync_days: input.sync_days, ...(input.password ? { password: input.password } : {}), ...(account.status === "disabled" ? { status: "active" } : {}) });
    } else {
      const created = await api.post<MailAccount>("/mail/accounts", input); setAccountSelection(created.id);
    }
    await refresh(); setAccountEditor(null); setNotice(remoteEnabled ? "Ящик сохранён. Проверьте подключение и запустите синхронизацию." : "Демонстрационный ящик сохранён. Соединение с сервером не выполнялось.");
  }
  async function saveCompose(compose: MailCompose, files: File[], send: boolean, draft: MailMessage | undefined, progress: (message: MailMessage, remainingFiles: File[]) => void): Promise<MailMessage> {
    if (!account) throw new Error("No account");
    let current: MailMessage;
    if (!remoteEnabled) {
      const forwarded = !draft && compose.forward_message_id ? demoMessages.find((item) => item.id === compose.forward_message_id)?.attachments ?? [] : [];
      current = { ...(draft ?? demoDraft(account, compose)), ...compose, version: (draft?.version ?? 0) + 1, attachments: [...(draft?.attachments ?? forwarded), ...files.map((file) => ({ id: crypto.randomUUID(), message_id: draft?.id ?? "demo", original_filename: file.name, content_type: file.type, size_bytes: file.size, sha256: "" }))], status: "draft" };
      storeMessage(current); progress(current, []);
      setNotice(send ? "Демонстрационный режим: письмо сохранено в черновиках, отправка не выполнялась." : "Черновик сохранён в текущем сеансе.");
      return current;
    }
    current = draft ? await api.patch<MailMessage>(`/mail/messages/${draft.id}/draft`, { ...compose, expected_version: draft.version }) : await api.post<MailMessage>(`/mail/accounts/${account.id}/drafts`, compose);
    storeMessage(current); progress(current, files);
    try {
      for (let index = 0; index < files.length; index += 1) {
        const form = new FormData(); form.append("file", files[index]);
        const attachment = await api.upload<MailAttachment>(`/mail/messages/${current.id}/attachments`, form);
        current = { ...current, attachments: [...current.attachments, attachment] };
        storeMessage(current); progress(current, files.slice(index + 1));
        // Upload may advance the draft version; use the authoritative record before sending.
        current = await api.get<MailMessage>(`/mail/messages/${current.id}`);
        storeMessage(current); progress(current, files.slice(index + 1));
      }
      if (send) {
        try { current = await api.post<MailMessage>(`/mail/messages/${current.id}/send`, { expected_version: current.version }); }
        catch (cause) {
          const reconciled = await api.get<MailMessage>(`/mail/messages/${current.id}`).catch(() => null);
          if (reconciled && ["queued", "sending", "sent", "uncertain"].includes(reconciled.status)) current = reconciled;
          else throw cause;
        }
      }
      storeMessage(current); progress(current, []);
      setNotice(send ? (current.status === "sent" ? "Письмо отправлено." : current.status === "uncertain" ? "Сервер не подтвердил результат отправки. Проверьте папку «Отправленные» перед повторной отправкой." : "Письмо поставлено в очередь отправки. Статус обновится автоматически.") : "Черновик сохранён.");
      return current;
    } finally { await refresh(); }
  }

  const queryError = accountsQuery.error ?? foldersQuery.error ?? messagesQuery.error;
  const loading = remoteEnabled && (accountsQuery.isPending || (!!account && (foldersQuery.isPending || messagesQuery.isPending)));
  const trashFolder = folders.find((folder) => folder.kind === "trash");
  const canEdit = selected && ["draft", "failed"].includes(selected.status);
  const canShare = selected && ["received", "sent"].includes(selected.status);

  return <section className="page mail-page">
    <header className="page-header"><div><h1>Почта</h1><p>Личная переписка и связь с вашими сделками</p></div><Button variant="primary" onClick={() => account ? setComposer({ initial: { ...blankCompose } }) : setAccountEditor("new")}><Plus size={17} />{account ? "Написать письмо" : "Подключить ящик"}</Button></header>
    {!remoteEnabled ? <div className="mail-demo-note"><ShieldCheck size={16} /><span>Демонстрационная почта · изменения не сохраняются. Отправка отключена.</span></div> : null}
    {notice ? <div role="status" className="mail-success">{notice}</div> : null}
    {error || queryError ? <div role="alert" className="mail-alert">{error || mailError(queryError)}{queryError ? <Button compact onClick={() => void refresh()}>Повторить загрузку</Button> : null}</div> : null}
    {loading && !account ? <div role="status" className="mail-empty">Загружаем почтовые ящики…</div> : !account ? <div className="mail-onboarding"><span className="mail-onboarding__icon"><Mail size={34} /></span><h2>Рабочая почта внутри CRM</h2><p>Подключите личный ящик Majordomo, чтобы читать письма, отвечать клиентам и сохранять нужную переписку в сделках.</p><Button variant="primary" onClick={() => setAccountEditor("new")}><Plus size={17} />Подключить почту</Button><small><ShieldCheck size={15} />Ваш ящик доступен только вам</small></div> : <>
      <div className="mail-account-bar"><div className="mail-account-choice"><Mail size={18} /><label className="sr-only" htmlFor="mail-account">Мой ящик</label><select id="mail-account" value={account.id} onChange={(event) => { setAccountSelection(event.target.value); setFolderSelection("inbox"); setSelectedId(null); }}>{accounts.map((item) => <option key={item.id} value={item.id}>{item.email}</option>)}</select><span className={`mail-account-status mail-account-status--${account.status}`}>{account.status === "active" ? "Подключён" : account.status === "degraded" ? "Нужна проверка" : "Отключён"}</span></div><div className="mail-account-actions"><Button compact disabled={pending} onClick={() => void run(async () => { if (!remoteEnabled) { setNotice("Демонстрационный режим: проверка соединения недоступна."); return; } const result = await api.post<{ healthy: boolean; detail: string | null }>(`/mail/accounts/${account.id}/check`, {}); setNotice(result.healthy ? "IMAP и SMTP доступны." : "Не удалось подключиться. Проверьте пароль и настройки серверов."); await refresh(); })}><CheckCheck size={15} />Проверить</Button><Button compact disabled={pending} onClick={() => void run(async () => { if (remoteEnabled) { await api.post(`/mail/accounts/${account.id}/sync`, {}); setNotice("Синхронизация поставлена в очередь. Письма появятся автоматически."); } else setNotice("Демонстрационный режим: синхронизация не выполнялась."); await refresh(); })}><RefreshCw size={15} />Обновить</Button><button className="icon-button" aria-label="Настройки моего ящика" onClick={() => setAccountEditor("edit")}><Settings2 size={18} /></button><button className="icon-button" aria-label="Добавить почтовый ящик" onClick={() => setAccountEditor("new")}><Plus size={18} /></button></div></div>
      {account.status === "disabled" ? <div className="mail-alert">Ящик отключён. Пароль удалён, синхронизация остановлена.<Button compact onClick={() => setAccountEditor("edit")}>Подключить повторно</Button></div> : null}
      {account.last_error ? <p className="mail-alert">Синхронизация временно недоступна. Проверьте соединение и настройки ящика.</p> : null}
      <div className={`mail-workspace${selectedId ? " mail-workspace--reading" : ""}`}>
        <aside className="mail-folders" aria-label="Папки почты"><button type="button" className={!folderSelection ? "mail-folder mail-folder--active" : "mail-folder"} onClick={() => changeFolder("")}><Mail size={17} /><span>Все письма</span></button>{folders.map((folder) => { const Icon = folderIcons[folder.kind] ?? Mail; return <button key={folder.id} className={`mail-folder${folderId === folder.id ? " mail-folder--active" : ""}`} onClick={() => changeFolder(folder.id)}><Icon size={17} /><span>{folder.display_name}</span>{folder.unread_count ? <b>{folder.unread_count}</b> : null}</button>; })}<div className="mail-privacy"><ShieldCheck size={17} /><span>Личный ящик<br />Доступен только вам</span></div><small className="mail-sync-time">{account.last_synced_at ? `Синхронизация: ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(account.last_synced_at))}` : "Ещё не синхронизирован"}</small></aside>
        <div className="mail-list"><div className="mail-mobile-folder"><label className="sr-only" htmlFor="mail-folder">Папка</label><select id="mail-folder" value={folderId ?? ""} onChange={(event) => changeFolder(event.target.value)}><option value="">Все письма</option>{folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.display_name}</option>)}</select><ChevronDown size={16} /></div><div className="mail-search"><Search size={17} /><input type="search" aria-label="Поиск по почте" placeholder="Поиск по почте" maxLength={200} value={search} onChange={(event) => { setSearch(event.target.value); setSelectedId(null); }} /></div><label className="mail-unread-filter"><input type="checkbox" checked={unread} onChange={(event) => { setUnread(event.target.checked); setSelectedId(null); }} />Только непрочитанные<span>{messages.length}{messagesQuery.hasNextPage ? "+" : ""}</span></label>
          <div className="mail-message-list" aria-label="Список писем">{loading && !messages.length ? <div className="mail-empty" role="status">Загружаем письма…</div> : messages.length ? messages.map((message) => <button key={message.id} className={`mail-row${selectedId === message.id ? " mail-row--selected" : ""}${!message.is_read ? " mail-row--unread" : ""}`} onClick={() => void openMessage(message)}><div className="mail-row__top"><strong>{message.direction === "outbound" ? `Кому: ${message.to_addresses.join(", ") || "не указан"}` : message.from_name || message.from_address}</strong><time>{messageDate(message).split(",")[0]}</time></div><div className="mail-row__subject">{!message.is_read ? <span className="mail-unread-dot" /> : null}<span>{message.subject || "Без темы"}</span>{message.is_starred ? <Star className="mail-starred" size={13} /> : null}{message.attachments.length ? <Paperclip size={13} /> : null}</div><p>{message.body_text || mailStatusLabels[message.status]}</p>{message.status !== "received" ? <small className={`mail-message-status mail-message-status--${message.status}`}>{mailStatusLabels[message.status]}</small> : null}</button>) : <div className="mail-empty"><Inbox size={27} /><p>{search || unread ? "Писем по этим условиям нет" : "В папке пока нет писем"}</p><small>{search || unread ? "Попробуйте изменить поиск или фильтр" : "Нажмите «Обновить», чтобы загрузить почту"}</small></div>}{messagesQuery.hasNextPage ? <Button className="mail-load-more" disabled={messagesQuery.isFetchingNextPage} onClick={() => void messagesQuery.fetchNextPage()}>{messagesQuery.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}</Button> : null}</div>
        </div>
        <article className="mail-reader" aria-label="Просмотр письма">
          {selectedId ? <button className="mail-back" onClick={() => setSelectedId(null)}><ArrowLeft size={18} />К письмам</button> : null}
          {selected ? <><div className="mail-reader-toolbar"><span className={`mail-message-status mail-message-status--${selected.status}`}>{mailStatusLabels[selected.status]}</span><div>{!canEdit && !["queued", "sending"].includes(selected.status) ? <select className="mail-move-select" aria-label="Переместить письмо" value={selected.folder_id} disabled={pending} onChange={(event) => { const destination = event.target.value; void run(async () => { await patchMessage(selected, { folder_id: destination }); setSelectedId(null); }); }}>{folders.filter((folder) => folder.kind !== "drafts" && !folder.name.startsWith("__")).map((folder) => <option key={folder.id} value={folder.id}>{folder.display_name}</option>)}</select> : null}<button className={`icon-button${selected.is_starred ? " mail-starred" : ""}`} disabled={pending} aria-label={selected.is_starred ? "Убрать звезду" : "Отметить звездой"} onClick={() => void run(() => patchMessage(selected, { is_starred: !selected.is_starred }))}><Star size={18} /></button><button className="icon-button" disabled={pending} aria-label={selected.is_read ? "Отметить непрочитанным" : "Отметить прочитанным"} onClick={() => void run(() => patchMessage(selected, { is_read: !selected.is_read }))}>{selected.is_read ? <Mail size={18} /> : <MailOpen size={18} />}</button>{selected.status === "draft" ? <button className="icon-button" disabled={pending} aria-label="Удалить черновик" onClick={() => void run(async () => { if (remoteEnabled) await api.delete(`/mail/messages/${selected.id}?expected_version=${selected.version}`); else setDemoMessages((current) => current.filter((item) => item.id !== selected.id)); setSelectedId(null); await refresh(); })}><Trash2 size={18} /></button> : trashFolder && selected.folder_id !== trashFolder.id && !["queued", "sending"].includes(selected.status) ? <button className="icon-button" disabled={pending} aria-label="Переместить в корзину" onClick={() => void run(async () => { await patchMessage(selected, { folder_id: trashFolder.id }); setSelectedId(null); })}><Trash2 size={18} /></button> : null}</div></div>
            <div className="mail-reader-content"><h2>{selected.subject || "Без темы"}</h2><div className="mail-sender"><span className="mail-sender__avatar">{(selected.from_name || selected.from_address).slice(0, 1).toLocaleUpperCase("ru")}</span><div><strong>{selected.from_name || selected.from_address}</strong>{selected.from_name ? <small>{selected.from_address}</small> : null}<small>Кому: {selected.to_addresses.join(", ") || "не указан"}</small>{selected.cc_addresses.length ? <small>Копия: {selected.cc_addresses.join(", ")}</small> : null}{selected.bcc_addresses.length ? <small>Скрытая копия: {selected.bcc_addresses.join(", ")}</small> : null}<time>{messageDate(selected)}</time></div></div>
              {selected.status === "uncertain" ? <div className="mail-alert">Результат отправки не подтверждён. Письмо могло быть доставлено. Проверьте папку «Отправленные» у провайдера перед повторной отправкой.</div> : null}
              {selected.status === "failed" ? <div className="mail-alert">Отправить письмо не удалось. Проверьте подключение и откройте письмо в редакторе для повторной отправки.</div> : null}
              {selected.last_error && !["failed", "uncertain"].includes(selected.status) ? <div className="mail-alert" role="status">{selected.last_error}</div> : null}
              {threadMessages.length > 1 ? <section className="mail-thread" aria-label="Письма цепочки"><h3>Письма цепочки · {threadMessages.length}{threadQuery.hasNextPage ? "+" : ""}</h3>{threadMessages.filter((message) => message.id !== selected.id).map((message) => <button type="button" key={message.id} onClick={() => void openMessage(message)}><span>{message.from_name || message.from_address}</span><strong>{message.subject || "Без темы"}</strong><time>{messageDate(message)}</time></button>)}{threadQuery.hasNextPage ? <Button compact disabled={threadQuery.isFetchingNextPage} onClick={() => void threadQuery.fetchNextPage()}>Ещё письма цепочки</Button> : null}</section> : null}
              <div className="mail-body">{selected.body_text || "У этого письма нет текстового содержимого."}</div>
              {selected.attachments.length ? <div className="mail-attachments"><h3>Вложения · {selected.attachments.length}</h3>{selected.attachments.map((attachment) => <button type="button" className="mail-attachment" key={attachment.id} disabled={pending} onClick={() => void run(async () => { if (remoteEnabled) await downloadMailAttachment(attachment); else setNotice("Демонстрационное вложение: файл не загружен на сервер."); })}><Paperclip size={18} /><span><strong>{attachment.original_filename}</strong><small>{fileSize(attachment.size_bytes)}</small></span><Download size={16} /></button>)}</div> : null}
              {canEdit ? <Button onClick={() => setComposer({ initial: composeFromMessage(selected, account, "edit"), draft: selected })}><Pencil size={16} />{selected.status === "failed" ? "Редактировать и повторить" : "Редактировать черновик"}</Button> : <div className="mail-reply-actions"><Button disabled={pending} onClick={() => setComposer({ initial: composeFromMessage(selected, account, "reply") })}><Reply size={16} />Ответить</Button><Button disabled={pending} onClick={() => setComposer({ initial: composeFromMessage(selected, account, "reply-all") })}><ReplyAll size={16} />Ответить всем</Button><Button disabled={pending} onClick={() => setComposer({ initial: composeFromMessage(selected, account, "forward"), forward: true, forwardedAttachments: selected.attachments })}><Forward size={16} />Переслать</Button></div>}
              {canShare ? <div className="mail-crm-link"><div><Link2 size={18} /><span><strong>Связь с CRM</strong><small>Поделитесь выбранным письмом в сделке</small></span></div><Button compact onClick={() => setLinking(true)}>Прикрепить к сделке</Button>{selected.linked_deal_ids.length ? <div className="mail-linked-deals">{selected.linked_deal_ids.map((id, index) => <Link key={id} to={`/deals?deal=${encodeURIComponent(id)}`}>Открыть связанную сделку {index + 1}</Link>)}</div> : null}</div> : null}
              {canShare ? <MailCrmActions key={selected.id} message={selected} account={account} onChanged={refresh} /> : null}
            </div></> : detailQuery.isError ? <div className="mail-empty" role="alert">{mailError(detailQuery.error)}<Button onClick={() => void detailQuery.refetch()}>Повторить</Button></div> : <div className="mail-reader-placeholder"><span><MailOpen size={33} /></span><h2>{selectedId && detailQuery.isFetching ? "Загружаем письмо…" : "Выберите письмо"}</h2><p>Здесь появится переписка, вложения<br />и связь с вашей CRM</p></div>}
        </article>
      </div>
    </>}
    {accountEditor ? <MailAccountEditor key={`${accountEditor}-${account?.id ?? "new"}`} account={accountEditor === "edit" ? account : undefined} onClose={() => setAccountEditor(null)} onSaved={saveAccount} onConflict={refresh} onDisconnect={accountEditor === "edit" && account ? async () => { if (remoteEnabled) await api.delete(`/mail/accounts/${account.id}?expected_version=${account.version}`); else setDemoAccounts((current) => current.filter((item) => item.id !== account.id)); setAccountSelection(""); setSelectedId(null); setAccountEditor(null); await refresh(); setNotice("Ящик отключён. Копии писем в сделках сохранены."); } : undefined} /> : null}
    {composer && account ? <MailComposer account={account} {...composer} onClose={() => setComposer(null)} onSave={saveCompose} onRemoveAttachment={async (message, attachment) => { if (remoteEnabled) { await api.delete(`/mail/attachments/${attachment.id}?expected_version=${message.version}`); const fresh = await api.get<MailMessage>(`/mail/messages/${message.id}`); storeMessage(fresh); await refresh(); return fresh; } const fresh = { ...message, attachments: message.attachments.filter((item) => item.id !== attachment.id), version: message.version + 1 }; storeMessage(fresh); return fresh; }} onConflict={async (id) => { const message = await api.get<MailMessage>(`/mail/messages/${id}`); storeMessage(message); await refresh(); return message; }} /> : null}
    {linking && selected ? <MailLinkDialog message={selected} onClose={() => setLinking(false)} onLink={async (dealId) => { if (remoteEnabled) await api.post(`/mail/messages/${selected.id}/link`, { deal_id: dealId }); else storeMessage({ ...selected, linked_deal_ids: [...new Set([...selected.linked_deal_ids, dealId])] }); await refresh(); setLinking(false); setNotice("Копия письма прикреплена к сделке."); }} /> : null}
  </section>;
}
