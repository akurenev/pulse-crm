import * as Dialog from "@radix-ui/react-dialog";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, ChevronLeft, ChevronRight, Pencil, Plus, RefreshCw, Search, Trash2, Users, X } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import { contacts, users } from "../data/demo";
import { api, remoteEnabled } from "../lib/api";
import { parseDealTags } from "../lib/deal-tags";
import { formatLongDate, formatMoney } from "../lib/format";
import type { ApiActivity, ApiCompany, ApiContact, ApiDeal, CursorPage } from "../types/api";
import type { Contact } from "../types/crm";

const CLIENTS_PAGE_SIZE = 25;
const CLIENT_SEARCH_DELAY_MS = 350;

function useDebouncedValue(value: string, delay: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedValue(value), delay);
    return () => window.clearTimeout(timeout);
  }, [delay, value]);

  return debouncedValue;
}

function listPagePath(collection: "contacts" | "companies", cursor: string | null, search: string) {
  const params = new URLSearchParams({ limit: String(CLIENTS_PAGE_SIZE) });
  if (cursor) params.set("cursor", cursor);
  if (search) params.set("search", search);
  return `/${collection}?${params.toString()}`;
}

function contactFromApi(contact: ApiContact): Contact {
  return {
    id: contact.id,
    name: `${contact.first_name} ${contact.last_name}`.trim(),
    company: contact.company_id ? "Компания" : "—",
    email: contact.primary_email ?? contact.emails[0] ?? "—",
    phone: contact.primary_phone ?? contact.phones[0] ?? "—",
    tags: contact.tags,
    deals: 0,
    revenue: 0,
    assignee: null,
  };
}

function demoContactNames(contact: Contact) {
  const [firstName = "", ...lastNameParts] = contact.name.trim().split(/\s+/);
  return { firstName, lastName: lastNameParts.join(" ") };
}

function replacePrimaryContactPoint(values: string[], previous: string | null, next: string) {
  const remaining = values.filter((value) => value !== previous && value !== next);
  return next ? [next, ...remaining] : remaining;
}

export default function ContactsPage() {
  const queryClient = useQueryClient();
  const [demoContacts, setDemoContacts] = useState<Contact[]>(contacts);
  const [demoCompanies, setDemoCompanies] = useState<ApiCompany[]>(() => Array.from(new Set(contacts.map((contact) => contact.company).filter((name) => name !== "—"))).map((name, index) => ({
    id: `demo-company-${index}`,
    name,
    website: null,
    phone: contacts.find((contact) => contact.company === name)?.phone ?? null,
    email: contacts.find((contact) => contact.company === name)?.email ?? null,
    tags: [],
    custom_fields: {},
    version: 1,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })));
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"contacts" | "companies">("contacts");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedContact, setSelectedContact] = useState<Contact | null>(null);
  const [selectedApiContact, setSelectedApiContact] = useState<ApiContact | null>(null);
  const [selectedCompany, setSelectedCompany] = useState<ApiCompany | null>(null);
  const [contactEditing, setContactEditing] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteError, setNoteError] = useState("");
  const [contactCursors, setContactCursors] = useState<Array<string | null>>([null]);
  const [companyCursors, setCompanyCursors] = useState<Array<string | null>>([null]);
  const normalizedSearch = search.trim().toLocaleLowerCase("ru");
  const debouncedSearch = useDebouncedValue(normalizedSearch, CLIENT_SEARCH_DELAY_MS);
  const contactCursor = contactCursors.at(-1) ?? null;
  const companyCursor = companyCursors.at(-1) ?? null;
  const remoteContacts = useQuery({
    queryKey: ["contacts", debouncedSearch, contactCursor],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(
      listPagePath("contacts", contactCursor, debouncedSearch),
      { signal },
    ),
    enabled: remoteEnabled && view === "contacts",
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  const remoteCompanies = useQuery({
    queryKey: ["companies", debouncedSearch, companyCursor],
    queryFn: ({ signal }) => api.get<CursorPage<ApiCompany>>(
      listPagePath("companies", companyCursor, debouncedSearch),
      { signal },
    ),
    enabled: remoteEnabled && view === "companies",
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  const sourceContacts = useMemo<Contact[]>(() => {
    if (!remoteEnabled) return demoContacts;
    return (remoteContacts.data?.items ?? []).map(contactFromApi);
  }, [demoContacts, remoteContacts.data]);
  const visibleContacts = useMemo(
    () => sourceContacts.filter((contact) => `${contact.name} ${contact.company} ${contact.email} ${contact.phone} ${contact.tags.join(" ")}`.toLocaleLowerCase("ru").includes(normalizedSearch)),
    [normalizedSearch, sourceContacts],
  );
  const sourceCompanies = useMemo<ApiCompany[]>(() => {
    if (remoteEnabled) return remoteCompanies.data?.items ?? [];
    return demoCompanies;
  }, [demoCompanies, remoteCompanies.data]);
  const visibleCompanies = useMemo(
    () => sourceCompanies.filter((company) => `${company.name} ${company.email ?? ""} ${company.phone ?? ""} ${company.tags.join(" ")}`.toLocaleLowerCase("ru").includes(normalizedSearch)),
    [normalizedSearch, sourceCompanies],
  );
  const loading = view === "contacts" ? remoteContacts.isLoading : remoteCompanies.isLoading;
  const failed = view === "contacts" ? remoteContacts.isError : remoteCompanies.isError;
  const fetching = view === "contacts" ? remoteContacts.isFetching : remoteCompanies.isFetching;
  const activeCursors = view === "contacts" ? contactCursors : companyCursors;
  const nextCursor = view === "contacts" ? remoteContacts.data?.next_cursor : remoteCompanies.data?.next_cursor;
  const currentPage = activeCursors.length;
  const showPagination = remoteEnabled && (currentPage > 1 || Boolean(nextCursor));
  const visibleRecordCount = view === "contacts" ? sourceContacts.length : sourceCompanies.length;
  const selectedEntityId = selectedContact?.id ?? selectedCompany?.id ?? null;
  const selectedEntityType = selectedContact ? "contact" : selectedCompany ? "company" : null;
  const detailActivityQuery = useQuery({
    queryKey: ["activity", selectedEntityType, selectedEntityId],
    queryFn: () => api.get<CursorPage<ApiActivity>>(`/activity?limit=20&entity_type=${selectedEntityType}&entity_id=${selectedEntityId}`),
    enabled: remoteEnabled && Boolean(selectedEntityId && selectedEntityType),
  });
  const purchasesQuery = useQuery({
    queryKey: ["contact-purchases", selectedContact?.id],
    queryFn: () => api.get<CursorPage<ApiDeal>>(`/contacts/${selectedContact?.id}/purchases?limit=100`),
    enabled: remoteEnabled && Boolean(selectedContact?.id),
  });
  const selectedPurchases = purchasesQuery.data?.items ?? [];
  const selectedRevenue = selectedPurchases.reduce((sum, deal) => sum + Number(deal.amount ?? 0), 0);
  const selectedDemoNames = selectedContact ? demoContactNames(selectedContact) : { firstName: "", lastName: "" };

  async function createEntity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setSaving(true);
    setSaveError("");
    try {
      if (remoteEnabled) {
        if (view === "contacts") {
          const email = String(data.get("email") ?? "").trim();
          const phone = String(data.get("phone") ?? "").trim();
          await api.post<ApiContact>("/contacts", {
            first_name: String(data.get("first_name") ?? "").trim(),
            last_name: String(data.get("last_name") ?? "").trim(),
            primary_email: email || null,
            primary_phone: phone || null,
            emails: email ? [email] : [],
            phones: phone ? [phone] : [],
            tags: [],
            custom_fields: {},
          });
          setContactCursors([null]);
          await queryClient.invalidateQueries({ queryKey: ["contacts"] });
        } else {
          const email = String(data.get("email") ?? "").trim();
          const phone = String(data.get("phone") ?? "").trim();
          const website = String(data.get("website") ?? "").trim();
          await api.post<ApiCompany>("/companies", {
            name: String(data.get("name") ?? "").trim(),
            email: email || null,
            phone: phone || null,
            website: website || null,
            tags: [],
            custom_fields: {},
          });
          setCompanyCursors([null]);
          await queryClient.invalidateQueries({ queryKey: ["companies"] });
        }
      } else if (view === "contacts") {
        const firstName = String(data.get("first_name") ?? "").trim();
        const lastName = String(data.get("last_name") ?? "").trim();
        setDemoContacts((items) => [{
          id: `contact-${crypto.randomUUID()}`,
          name: `${firstName} ${lastName}`.trim(),
          company: "—",
          email: String(data.get("email") ?? "").trim() || "—",
          phone: String(data.get("phone") ?? "").trim() || "—",
          tags: [],
          deals: 0,
          revenue: 0,
          assignee: users.ak,
        }, ...items]);
      } else {
        const now = new Date().toISOString();
        setDemoCompanies((items) => [{
          id: `company-${crypto.randomUUID()}`,
          name: String(data.get("name") ?? "").trim(),
          email: String(data.get("email") ?? "").trim() || null,
          phone: String(data.get("phone") ?? "").trim() || null,
          website: String(data.get("website") ?? "").trim() || null,
          tags: [],
          custom_fields: {},
          version: 1,
          created_at: now,
          updated_at: now,
        }, ...items]);
      }
      form.reset();
      setDialogOpen(false);
    } catch {
      setSaveError("Не удалось сохранить запись. Проверьте заполненные поля.");
    } finally {
      setSaving(false);
    }
  }

  function openContact(contact: Contact) {
    setSelectedContact(contact);
    setSelectedApiContact(remoteContacts.data?.items.find((item) => item.id === contact.id) ?? null);
    setSelectedCompany(null);
    setContactEditing(false);
    setSaveError("");
  }

  function closeRecord() {
    setSelectedContact(null);
    setSelectedApiContact(null);
    setSelectedCompany(null);
    setContactEditing(false);
    setSaveError("");
  }

  function updateContactQueryCache(updated: ApiContact) {
    queryClient.setQueriesData<CursorPage<ApiContact>>({ queryKey: ["contacts"] }, (page) => page ? {
      ...page,
      items: page.items.map((contact) => contact.id === updated.id ? updated : contact),
    } : page);
  }

  function removeContactFromQueryCache(contactId: string) {
    queryClient.setQueriesData<CursorPage<ApiContact>>({ queryKey: ["contacts"] }, (page) => page ? {
      ...page,
      items: page.items.filter((contact) => contact.id !== contactId),
    } : page);
  }

  async function updateContact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedContact) return;
    const data = new FormData(event.currentTarget);
    const firstName = String(data.get("first_name") ?? "").trim();
    const lastName = String(data.get("last_name") ?? "").trim();
    const email = String(data.get("email") ?? "").trim();
    const phone = String(data.get("phone") ?? "").trim();
    const tags = parseDealTags(String(data.get("tags") ?? ""));
    setSaving(true);
    setSaveError("");
    try {
      if (remoteEnabled) {
        if (!selectedApiContact) throw new Error("Contact version is unavailable");
        const updated = await api.patch<ApiContact>(`/contacts/${selectedContact.id}`, {
          expected_version: selectedApiContact.version,
          first_name: firstName,
          last_name: lastName,
          primary_email: email || null,
          primary_phone: phone || null,
          emails: replacePrimaryContactPoint(selectedApiContact.emails, selectedApiContact.primary_email, email),
          phones: replacePrimaryContactPoint(selectedApiContact.phones, selectedApiContact.primary_phone, phone),
          tags,
        });
        updateContactQueryCache(updated);
        setSelectedApiContact(updated);
        setSelectedContact(contactFromApi(updated));
        await queryClient.invalidateQueries({ queryKey: ["contacts"] });
      } else {
        const updated: Contact = {
          ...selectedContact,
          name: `${firstName} ${lastName}`.trim(),
          email: email || "—",
          phone: phone || "—",
          tags,
        };
        setDemoContacts((items) => items.map((contact) => contact.id === updated.id ? updated : contact));
        setSelectedContact(updated);
      }
      setContactEditing(false);
      setNotice("Контакт обновлён");
    } catch {
      setSaveError("Не удалось обновить контакт. Возможно, запись уже была изменена.");
    } finally {
      setSaving(false);
    }
  }

  async function deleteContact() {
    if (!selectedContact) return;
    setDeleting(true);
    setDeleteError("");
    try {
      if (remoteEnabled) {
        if (!selectedApiContact) throw new Error("Contact version is unavailable");
        await api.delete(`/contacts/${selectedContact.id}?expected_version=${selectedApiContact.version}`);
        removeContactFromQueryCache(selectedContact.id);
        await queryClient.invalidateQueries({ queryKey: ["contacts"] });
      } else {
        setDemoContacts((items) => items.filter((contact) => contact.id !== selectedContact.id));
      }
      setDeleteConfirmOpen(false);
      closeRecord();
      setNotice("Контакт удалён");
    } catch {
      setDeleteError("Не удалось удалить контакт. Обновите страницу и попробуйте ещё раз.");
    } finally {
      setDeleting(false);
    }
  }

  function updateSearch(value: string) {
    setSearch(value);
    setContactCursors([null]);
    setCompanyCursors([null]);
  }

  function showNextPage() {
    if (!nextCursor) return;
    if (view === "contacts") {
      setContactCursors((cursors) => cursors.at(-1) === nextCursor ? cursors : [...cursors, nextCursor]);
    } else {
      setCompanyCursors((cursors) => cursors.at(-1) === nextCursor ? cursors : [...cursors, nextCursor]);
    }
  }

  function showPreviousPage() {
    const previous = (cursors: Array<string | null>) => cursors.length > 1 ? cursors.slice(0, -1) : cursors;
    if (view === "contacts") {
      setContactCursors(previous);
    } else {
      setCompanyCursors(previous);
    }
  }

  function retryActiveList() {
    if (view === "contacts") {
      void remoteContacts.refetch();
    } else {
      void remoteCompanies.refetch();
    }
  }

  async function createNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = String(new FormData(form).get("note") ?? "").trim();
    if (!body || !selectedEntityId || !selectedEntityType) return;
    if (!remoteEnabled) {
      setNoteError("Заметки сохраняются после подключения API");
      return;
    }
    const collection = selectedEntityType === "contact" ? "contacts" : "companies";
    setNoteSaving(true);
    setNoteError("");
    try {
      await api.post<ApiActivity>(`/${collection}/${selectedEntityId}/notes`, { body });
      form.reset();
      await queryClient.invalidateQueries({ queryKey: ["activity", selectedEntityType, selectedEntityId] });
    } catch {
      setNoteError("Не удалось сохранить заметку");
    } finally {
      setNoteSaving(false);
    }
  }

  return (
    <div className="page contacts-page">
      {notice ? <button className="toast" type="button" onClick={() => setNotice(null)}>{notice}</button> : null}
      <header className="page-header">
        <div><h1>Клиенты</h1><p>{remoteEnabled ? `Страница ${currentPage} · ${visibleRecordCount} записей` : `${visibleRecordCount} записей в рабочей базе`}</p></div>
        <Button className="contacts-page__desktop-add" variant="primary" aria-controls="new-client-dialog" onClick={() => setDialogOpen(true)}><Plus size={17} /> {view === "contacts" ? "Новый контакт" : "Новая компания"}</Button>
      </header>
      <div className="content-toolbar">
        <label className="search-control"><Search size={18} /><input value={search} onChange={(event) => updateSearch(event.target.value)} placeholder="Имя, компания, тег, телефон или email" /></label>
        <div className="view-switch" aria-label="Тип клиентов"><button className={view === "contacts" ? "is-active" : ""} type="button" aria-label="Контакты" onClick={() => setView("contacts")}><Users size={18} /></button><button className={view === "companies" ? "is-active" : ""} type="button" aria-label="Компании" onClick={() => setView("companies")}><Building2 size={18} /></button></div>
      </div>

      {loading ? <div className="route-loading" role="status">Загружаем клиентов…</div> : null}
      {failed ? <div className="load-error load-error--action" role="alert">
        <span>Не удалось загрузить {view === "contacts" ? "контакты" : "компании"}. Проверьте соединение и попробуйте ещё раз.</span>
        <Button compact onClick={retryActiveList} disabled={fetching}><RefreshCw size={15} /> {fetching ? "Повторяем…" : "Повторить"}</Button>
      </div> : null}
      {!loading && !failed && view === "contacts" ? <section className="data-table" role="region" aria-label="Список контактов">
        <div className="data-table__header"><span>Клиент</span><span>Контакты</span><span>Сделки</span><span>Следующая покупка</span><span>Ответственный</span></div>
        {visibleContacts.map((contact) => (
          <button className="data-row" type="button" key={contact.id} onClick={() => openContact(contact)}>
            <span className="data-row__primary"><span className="company-avatar">{contact.name.slice(0, 1)}</span><span><strong>{contact.name}</strong><small>{[...(contact.company === "—" ? [] : [contact.company]), ...contact.tags].join(" · ") || "—"}</small></span></span>
            <span className="data-row__contact"><strong>{contact.phone}</strong><small>{contact.email}</small></span>
            <span>{remoteEnabled ? <><strong>—</strong><small>см. карточку</small></> : <><strong>{contact.deals}</strong><small>{formatMoney(contact.revenue)}</small></>}</span>
            <span>{contact.nextPurchaseAt ? formatLongDate(contact.nextPurchaseAt) : "Не запланирована"}</span>
            {contact.assignee ? <span className="owner-line"><Avatar user={contact.assignee} size="sm" /><span>{contact.assignee.name}</span></span> : <span className="owner-line owner-line--unassigned">Не назначен</span>}
          </button>
        ))}
      </section> : null}
      {!loading && !failed && view === "companies" ? <section className="data-table" role="region" aria-label="Список компаний">
        <div className="data-table__header"><span>Компания</span><span>Контакты</span><span>Теги</span><span>Сайт</span><span>Обновлено</span></div>
        {visibleCompanies.map((company) => (
          <button className="data-row" type="button" key={company.id} onClick={() => setSelectedCompany(company)}>
            <span className="data-row__primary"><span className="company-avatar">{company.name.slice(0, 1)}</span><span><strong>{company.name}</strong><small>Компания</small></span></span>
            <span className="data-row__contact"><strong>{company.phone ?? "—"}</strong><small>{company.email ?? "—"}</small></span>
            <span><strong>{company.tags.length}</strong><small>тегов</small></span>
            <span>{company.website ?? "Не указан"}</span>
            <span>{formatLongDate(company.updated_at)}</span>
          </button>
        ))}
      </section> : null}
      {!loading && showPagination ? <nav className="list-pagination" aria-label={view === "contacts" ? "Пагинация контактов" : "Пагинация компаний"}>
        <Button compact className="list-pagination__button" onClick={showPreviousPage} disabled={currentPage === 1 || fetching} aria-label="Предыдущая страница"><ChevronLeft size={16} /> Назад</Button>
        <span className="list-pagination__status" aria-live="polite">Страница {currentPage}</span>
        <Button compact className="list-pagination__button" onClick={showNextPage} disabled={!nextCursor || fetching} aria-label="Следующая страница">Далее <ChevronRight size={16} /></Button>
      </nav> : null}
      <button
        className="mobile-fab contacts-page__mobile-add"
        type="button"
        aria-label={view === "contacts" ? "Добавить контакт" : "Добавить компанию"}
        aria-controls="new-client-dialog"
        onClick={() => setDialogOpen(true)}
      >
        <Plus aria-hidden="true" size={24} />
      </button>

      <Dialog.Root open={dialogOpen} onOpenChange={setDialogOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content id="new-client-dialog" className="dialog-content">
            <div className="dialog-header"><div><Dialog.Title>{view === "contacts" ? "Новый контакт" : "Новая компания"}</Dialog.Title><Dialog.Description>Запись будет доступна всей команде.</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close></div>
            <form className="form-stack" onSubmit={(event) => void createEntity(event)}>
              {view === "contacts" ? <><label className="field"><span>Имя</span><input name="first_name" required autoFocus /></label><label className="field"><span>Фамилия</span><input name="last_name" /></label></> : <label className="field"><span>Название</span><input name="name" required autoFocus /></label>}
              <label className="field"><span>Email</span><input name="email" type="email" /></label>
              <label className="field"><span>Телефон</span><input name="phone" type="tel" /></label>
              {view === "companies" ? <label className="field"><span>Сайт</span><input name="website" type="url" placeholder="https://" /></label> : null}
              {saveError ? <p className="form-error" role="alert">{saveError}</p> : null}
              <div className="dialog-actions"><Dialog.Close asChild><Button type="button">Отмена</Button></Dialog.Close><Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : "Сохранить"}</Button></div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={Boolean(selectedContact || selectedCompany)}
        onOpenChange={(open) => {
          if (!open) {
            closeRecord();
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content className="dialog-content record-dialog">
            <div className="dialog-header record-dialog__header">
              <div>
                <Dialog.Title>{selectedContact?.name ?? selectedCompany?.name ?? "Карточка клиента"}</Dialog.Title>
                <Dialog.Description>{selectedContact ? "Контакт" : "Компания"}</Dialog.Description>
              </div>
              <div className="record-dialog__header-actions">
                {selectedContact && !contactEditing ? <>
                  <Button compact aria-label="Редактировать контакт" onClick={() => { setSaveError(""); setContactEditing(true); }}><Pencil size={15} /><span className="record-dialog__action-label">Редактировать</span></Button>
                  <Button compact variant="danger" aria-label="Удалить контакт" onClick={() => { setDeleteError(""); setDeleteConfirmOpen(true); }}><Trash2 size={15} /><span className="record-dialog__action-label">Удалить</span></Button>
                </> : null}
                <Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close>
              </div>
            </div>
            {selectedContact && contactEditing ? <form className="form-stack contact-edit-form" onSubmit={(event) => void updateContact(event)}>
              <label className="field"><span>Имя</span><input name="first_name" required autoFocus defaultValue={selectedApiContact?.first_name ?? selectedDemoNames.firstName} /></label>
              <label className="field"><span>Фамилия</span><input name="last_name" defaultValue={selectedApiContact?.last_name ?? selectedDemoNames.lastName} /></label>
              <label className="field"><span>Email</span><input name="email" type="email" defaultValue={selectedApiContact?.primary_email ?? (selectedContact.email === "—" ? "" : selectedContact.email)} /></label>
              <label className="field"><span>Телефон</span><input name="phone" type="tel" defaultValue={selectedApiContact?.primary_phone ?? (selectedContact.phone === "—" ? "" : selectedContact.phone)} /></label>
              <label className="field"><span>Теги</span><input name="tags" defaultValue={(selectedApiContact?.tags ?? selectedContact.tags).join(", ")} placeholder="Например: VIP, постоянный клиент" /></label>
              {saveError ? <p className="form-error" role="alert">{saveError}</p> : null}
              <div className="dialog-actions"><Button type="button" onClick={() => { setSaveError(""); setContactEditing(false); }}>Отмена</Button><Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : "Сохранить"}</Button></div>
            </form> : null}
            {selectedContact && !contactEditing ? <div className="record-detail-grid">
              <div><small>Телефон</small><strong>{selectedContact.phone}</strong></div>
              <div><small>Email</small><strong>{selectedContact.email}</strong></div>
              <div><small>Компания</small><strong>{selectedContact.company}</strong></div>
              <div><small>Ответственный</small><strong>{selectedContact.assignee?.name ?? "Не назначен"}</strong></div>
              <div className="record-detail-grid__wide"><small>Теги</small><strong>{selectedContact.tags.length ? selectedContact.tags.join(", ") : "Нет тегов"}</strong></div>
              <div><small>Покупок</small><strong>{remoteEnabled ? selectedPurchases.length : selectedContact.deals}</strong></div>
              <div><small>Покупок на сумму</small><strong>{formatMoney(remoteEnabled ? selectedRevenue : selectedContact.revenue)}</strong></div>
              <div className="record-detail-grid__wide"><small>Следующая покупка</small><strong>{selectedContact.nextPurchaseAt ? formatLongDate(selectedContact.nextPurchaseAt) : "Не запланирована"}</strong></div>
            </div> : null}
            {selectedCompany && !contactEditing ? <div className="record-detail-grid">
              <div><small>Телефон</small><strong>{selectedCompany.phone ?? "—"}</strong></div>
              <div><small>Email</small><strong>{selectedCompany.email ?? "—"}</strong></div>
              <div className="record-detail-grid__wide"><small>Сайт</small><strong>{selectedCompany.website ?? "Не указан"}</strong></div>
              <div><small>Теги</small><strong>{selectedCompany.tags.length ? selectedCompany.tags.join(", ") : "Нет тегов"}</strong></div>
              <div><small>Обновлено</small><strong>{formatLongDate(selectedCompany.updated_at)}</strong></div>
            </div> : null}
            {selectedContact && !contactEditing ? <section className="record-purchases">
              <h3>История покупок</h3>
              {purchasesQuery.isLoading ? <p className="empty-copy">Загружаем покупки…</p> : null}
              {selectedPurchases.map((deal) => <article key={deal.id}><span><strong>{deal.title}</strong><small>{formatLongDate(deal.updated_at)}</small></span><b>{formatMoney(Number(deal.amount ?? 0))}</b></article>)}
              {!remoteEnabled || (!purchasesQuery.isLoading && !selectedPurchases.length) ? <p className="empty-copy">Выигранные сделки появятся здесь как покупки.</p> : null}
            </section> : null}
            {!contactEditing ? <section className="record-timeline">
              <h3>История</h3>
              <form className="note-composer record-note-composer" onSubmit={(event) => void createNote(event)}>
                <textarea name="note" rows={2} maxLength={10_000} placeholder="Добавить заметку" aria-label="Текст заметки" />
                <button type="submit" disabled={noteSaving}>{noteSaving ? "Сохраняем…" : "Добавить"}</button>
                {noteError ? <small className="message-error" role="alert">{noteError}</small> : null}
              </form>
              {detailActivityQuery.isLoading ? <p className="empty-copy">Загружаем события…</p> : null}
              {(detailActivityQuery.data?.items ?? []).map((event) => <article key={event.id}><span /><div><strong>{event.event_type.replaceAll(".", " · ")}</strong>{typeof event.payload.body === "string" ? <p>{event.payload.body}</p> : null}<small>{new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(event.occurred_at))}</small></div></article>)}
              {!remoteEnabled || (!detailActivityQuery.isLoading && !detailActivityQuery.data?.items.length) ? <p className="empty-copy">История появится после изменений, сделок и сообщений.</p> : null}
            </section> : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={deleteConfirmOpen} onOpenChange={(open) => { if (!deleting) { setDeleteConfirmOpen(open); if (!open) setDeleteError(""); } }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay dialog-overlay--confirm" />
          <Dialog.Content className="dialog-content confirm-dialog">
            <div className="dialog-header">
              <div><Dialog.Title>Удалить контакт?</Dialog.Title><Dialog.Description>«{selectedContact?.name}» будет удалён из списка. Это действие нельзя отменить.</Dialog.Description></div>
              <Dialog.Close className="icon-button" aria-label="Закрыть подтверждение" disabled={deleting}><X size={20} /></Dialog.Close>
            </div>
            {deleteError ? <p className="form-error" role="alert">{deleteError}</p> : null}
            <div className="dialog-actions"><Dialog.Close asChild><Button type="button" disabled={deleting}>Отмена</Button></Dialog.Close><Button type="button" variant="danger" disabled={deleting} onClick={() => void deleteContact()}>{deleting ? "Удаляем…" : "Удалить контакт"}</Button></div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
