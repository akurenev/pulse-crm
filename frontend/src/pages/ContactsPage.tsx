import * as Dialog from "@radix-ui/react-dialog";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, ChevronLeft, ChevronRight, ExternalLink, Pencil, Plus, RefreshCw, Search, Trash2, Users, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import { contacts, users } from "../data/demo";
import { ApiError, api, remoteEnabled } from "../lib/api";
import { deepLinkEntityId } from "../lib/deep-links";
import { parseDealTags } from "../lib/deal-tags";
import { formatLongDate, formatMoney } from "../lib/format";
import { useCrm } from "../state/crm-store";
import type { ApiActivity, ApiCompany, ApiContact, ApiDeal, ApiUser, CursorPage } from "../types/api";
import type { Contact, UserSummary } from "../types/crm";

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

function apiUserSummary(user: ApiUser): UserSummary {
  return {
    id: user.id,
    name: user.full_name,
    initials: user.full_name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toLocaleUpperCase("ru"),
    tone: "violet",
  };
}

function contactFromApi(contact: ApiContact, usersById: ReadonlyMap<string, UserSummary>): Contact {
  return {
    id: contact.id,
    name: `${contact.first_name} ${contact.last_name}`.trim(),
    company: contact.company_id ? "Компания" : "—",
    email: contact.primary_email ?? contact.emails[0] ?? "—",
    phone: contact.primary_phone ?? contact.phones[0] ?? "—",
    tags: contact.tags,
    deals: 0,
    revenue: 0,
    assignee: contact.assignee_id ? usersById.get(contact.assignee_id) ?? null : null,
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
  const { currentUser, isEmployee } = useCrm();
  const queryClient = useQueryClient();
  const [routeSearchParams, setRouteSearchParams] = useSearchParams();
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
  const [companyEditing, setCompanyEditing] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteError, setNoteError] = useState("");
  const [accessRevision, setAccessRevision] = useState(0);
  const [contactCursors, setContactCursors] = useState<Array<string | null>>([null]);
  const [companyCursors, setCompanyCursors] = useState<Array<string | null>>([null]);
  const accessGeneration = useRef(0);
  const normalizedSearch = search.trim().toLocaleLowerCase("ru");
  const debouncedSearch = useDebouncedValue(normalizedSearch, CLIENT_SEARCH_DELAY_MS);
  const contactCursor = contactCursors.at(-1) ?? null;
  const companyCursor = companyCursors.at(-1) ?? null;
  const routedContactId = deepLinkEntityId(routeSearchParams, "contact");
  const routedCompanyId = deepLinkEntityId(routeSearchParams, "company");
  const remoteContacts = useQuery({
    queryKey: ["contacts", debouncedSearch, contactCursor, accessRevision],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(
      listPagePath("contacts", contactCursor, debouncedSearch),
      { signal },
    ),
    enabled: remoteEnabled && view === "contacts",
    placeholderData: accessRevision > 0 ? undefined : keepPreviousData,
    staleTime: 30_000,
  });
  const remoteUsers = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<ApiUser[]>("/users"),
    enabled: remoteEnabled,
  });
  const remoteCompanies = useQuery({
    queryKey: ["companies", debouncedSearch, companyCursor, accessRevision],
    queryFn: ({ signal }) => api.get<CursorPage<ApiCompany>>(
      listPagePath("companies", companyCursor, debouncedSearch),
      { signal },
    ),
    enabled: remoteEnabled && !isEmployee && view === "companies",
    placeholderData: accessRevision > 0 ? undefined : keepPreviousData,
    staleTime: 30_000,
  });
  const routedContactFromPage = remoteContacts.data?.items.find((contact) => contact.id === routedContactId) ?? null;
  const routedCompanyFromPage = remoteCompanies.data?.items.find((company) => company.id === routedCompanyId) ?? null;
  const routedContactQuery = useQuery({
    queryKey: ["contact-detail", routedContactId],
    queryFn: ({ signal }) => api.get<ApiContact>(`/contacts/${encodeURIComponent(routedContactId ?? "")}`, { signal }),
    enabled: remoteEnabled && Boolean(routedContactId && !routedContactFromPage && selectedApiContact?.id !== routedContactId),
  });
  const routedCompanyQuery = useQuery({
    queryKey: ["company-detail", routedCompanyId],
    queryFn: ({ signal }) => api.get<ApiCompany>(`/companies/${encodeURIComponent(routedCompanyId ?? "")}`, { signal }),
    enabled: remoteEnabled && !isEmployee && Boolean(routedCompanyId && !routedCompanyFromPage && selectedCompany?.id !== routedCompanyId),
  });
  const sourceContacts = useMemo<Contact[]>(() => {
    if (!remoteEnabled) return demoContacts;
    const usersById = new Map((remoteUsers.data ?? []).map((user) => [user.id, apiUserSummary(user)]));
    usersById.set(currentUser.id, currentUser);
    return (remoteContacts.data?.items ?? []).map((contact) => contactFromApi(contact, usersById));
  }, [currentUser, demoContacts, remoteContacts.data, remoteUsers.data]);
  const visibleContacts = useMemo(
    () => sourceContacts.filter((contact) => `${contact.name} ${contact.company} ${contact.email} ${contact.phone} ${contact.tags.join(" ")}`.toLocaleLowerCase("ru").includes(normalizedSearch)),
    [normalizedSearch, sourceContacts],
  );
  const sourceCompanies = useMemo<ApiCompany[]>(() => {
    if (remoteEnabled) return remoteCompanies.data?.items ?? [];
    return demoCompanies;
  }, [demoCompanies, remoteCompanies.data]);
  const visibleCompanies = useMemo(
    () => remoteEnabled
      ? sourceCompanies
      : sourceCompanies.filter((company) => `${company.name} ${company.email ?? ""} ${company.phone ?? ""} ${company.tags.join(" ")}`.toLocaleLowerCase("ru").includes(normalizedSearch)),
    [normalizedSearch, sourceCompanies],
  );
  const loading = view === "contacts" ? remoteContacts.isLoading || remoteUsers.isLoading : remoteCompanies.isLoading;
  const failed = view === "contacts" ? remoteContacts.isError || remoteUsers.isError : remoteCompanies.isError;
  const fetching = view === "contacts" ? remoteContacts.isFetching || remoteUsers.isFetching : remoteCompanies.isFetching;
  const activeCursors = view === "contacts" ? contactCursors : companyCursors;
  const nextCursor = view === "contacts" ? remoteContacts.data?.next_cursor : remoteCompanies.data?.next_cursor;
  const currentPage = activeCursors.length;
  const showPagination = remoteEnabled && (currentPage > 1 || Boolean(nextCursor));
  const visibleRecordCount = view === "contacts" ? sourceContacts.length : sourceCompanies.length;
  const selectedEntityId = selectedContact?.id ?? selectedCompany?.id ?? null;
  const selectedEntityType = selectedContact ? "contact" : selectedCompany ? "company" : null;
  const detailActivityQuery = useQuery({
    queryKey: ["activity", selectedEntityType, selectedEntityId],
    queryFn: ({ signal }) => api.get<CursorPage<ApiActivity>>(
      `/activity?limit=20&entity_type=${selectedEntityType}&entity_id=${selectedEntityId}`,
      { signal },
    ),
    enabled: remoteEnabled && Boolean(selectedEntityId && selectedEntityType),
  });
  const purchasesQuery = useQuery({
    queryKey: ["contact-purchases", selectedContact?.id],
    queryFn: ({ signal }) => api.get<CursorPage<ApiDeal>>(
      `/contacts/${selectedContact?.id}/purchases?limit=100`,
      { signal },
    ),
    enabled: remoteEnabled && Boolean(selectedContact?.id),
  });
  const contactDealsQuery = useQuery({
    queryKey: ["contact-deals", selectedContact?.id],
    queryFn: ({ signal }) => api.get<CursorPage<ApiDeal>>(
      `/contacts/${selectedContact?.id}/deals?limit=100`,
      { signal },
    ),
    enabled: remoteEnabled && Boolean(selectedContact?.id),
  });
  const selectedContactCompanyId = selectedApiContact?.company_id ?? null;
  const selectedContactCompanyQuery = useQuery({
    queryKey: ["company-detail", selectedContactCompanyId],
    queryFn: ({ signal }) => api.get<ApiCompany>(`/companies/${selectedContactCompanyId}`, { signal }),
    enabled: remoteEnabled && !isEmployee && Boolean(selectedContact && selectedContactCompanyId),
  });
  const companyContactsQuery = useQuery({
    queryKey: ["company-contacts", selectedCompany?.id],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(
      `/companies/${selectedCompany?.id}/contacts?limit=100`,
      { signal },
    ),
    enabled: remoteEnabled && !isEmployee && Boolean(selectedCompany?.id),
  });
  const selectedPurchases = purchasesQuery.data?.items ?? [];
  const selectedRevenue = selectedPurchases.reduce((sum, deal) => sum + Number(deal.amount ?? 0), 0);
  const selectedDemoNames = selectedContact ? demoContactNames(selectedContact) : { firstName: "", lastName: "" };
  const availableAssignees = useMemo(() => {
    const remote = (remoteUsers.data ?? []).map(apiUserSummary);
    const candidates = remoteEnabled ? remote : Object.values(users);
    return candidates.some((user) => user.id === currentUser.id) ? candidates : [currentUser, ...candidates];
  }, [currentUser, remoteUsers.data]);
  const selectedContactCompany = selectedContactCompanyQuery.data
    ?? (!remoteEnabled && selectedContact
      ? demoCompanies.find((company) => company.name === selectedContact.company) ?? null
      : null);
  const companyContacts = useMemo<Contact[]>(() => {
    if (!selectedCompany) return [];
    if (!remoteEnabled) return demoContacts.filter((contact) => contact.company === selectedCompany.name);
    const usersById = new Map(availableAssignees.map((user) => [user.id, user]));
    return (companyContactsQuery.data?.items ?? []).map((contact) => contactFromApi(contact, usersById));
  }, [availableAssignees, companyContactsQuery.data, demoContacts, selectedCompany]);
  const selectedContactDeals = remoteEnabled ? contactDealsQuery.data?.items ?? [] : [];

  useEffect(() => {
    if (!isEmployee || view === "contacts") return;
    setView("contacts");
    setSelectedCompany(null);
    setDialogOpen(false);
  }, [isEmployee, view]);

  useEffect(() => {
    if (!routedContactId) return;
    if (remoteEnabled) {
      const apiContact = routedContactFromPage ?? routedContactQuery.data;
      if (!apiContact) return;
      const usersById = new Map(availableAssignees.map((user) => [user.id, user]));
      const mappedContact = contactFromApi(apiContact, usersById);
      setSelectedContact(mappedContact);
      setSelectedApiContact(apiContact);
      setSelectedCompany(null);
      setContactEditing(false);
      setCompanyEditing(false);
      setSaveError("");
      return;
    }
    const demoContact = demoContacts.find((contact) => contact.id === routedContactId);
    if (demoContact) {
      setSelectedContact(demoContact);
      setSelectedApiContact(null);
      setSelectedCompany(null);
      setContactEditing(false);
      setCompanyEditing(false);
      setSaveError("");
    }
  }, [availableAssignees, demoContacts, routedContactFromPage, routedContactId, routedContactQuery.data]);

  useEffect(() => {
    if (routedContactId || !routedCompanyId || isEmployee) return;
    const company = remoteEnabled
      ? routedCompanyFromPage ?? routedCompanyQuery.data
      : demoCompanies.find((item) => item.id === routedCompanyId);
    if (!company) return;
    setSelectedContact(null);
    setSelectedApiContact(null);
    setSelectedCompany(company);
    setContactEditing(false);
    setCompanyEditing(false);
    setSaveError("");
  }, [demoCompanies, isEmployee, routedCompanyFromPage, routedCompanyId, routedCompanyQuery.data, routedContactId]);

  useEffect(() => {
    if (!remoteEnabled) return;
    const handleAccessChanged = () => {
      accessGeneration.current += 1;
      const contactId = selectedContact?.id;
      if (selectedEntityId && selectedEntityType) {
        queryClient.removeQueries({ queryKey: ["activity", selectedEntityType, selectedEntityId] });
      }
      if (contactId) {
        queryClient.removeQueries({ queryKey: ["contact-purchases", contactId] });
        queryClient.removeQueries({ queryKey: ["contact-deals", contactId] });
      }
      queryClient.removeQueries({ queryKey: ["contact-detail"] });
      queryClient.removeQueries({ queryKey: ["company-detail"] });
      queryClient.removeQueries({ queryKey: ["company-contacts"] });
      setRouteSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("contact");
        next.delete("company");
        return next;
      }, { replace: true });
      setSelectedContact(null);
      setSelectedApiContact(null);
      setSelectedCompany(null);
      setDialogOpen(false);
      setContactEditing(false);
      setCompanyEditing(false);
      setDeleteConfirmOpen(false);
      setDeleteError("");
      setSaveError("");
      setContactCursors([null]);
      setCompanyCursors([null]);
      setAccessRevision((value) => value + 1);
    };
    window.addEventListener("pulse:access-changed", handleAccessChanged);
    return () => window.removeEventListener("pulse:access-changed", handleAccessChanged);
  }, [queryClient, selectedContact?.id, selectedEntityId, selectedEntityType, setRouteSearchParams]);

  async function createEntity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (isEmployee && view === "companies") return;
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
            ...(!isEmployee ? { assignee_id: String(data.get("assignee_id") ?? "") || null } : {}),
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
          assignee: isEmployee
            ? currentUser
            : availableAssignees.find((user) => user.id === String(data.get("assignee_id") ?? "")) ?? currentUser,
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

  function routeToRecord(kind: "contact" | "company", id: string) {
    const next = new URLSearchParams(routeSearchParams);
    next.delete(kind === "contact" ? "company" : "contact");
    next.set(kind, id);
    setRouteSearchParams(next);
  }

  function openContact(contact: Contact, apiContact?: ApiContact | null) {
    setSelectedContact(contact);
    setSelectedApiContact(apiContact ?? remoteContacts.data?.items.find((item) => item.id === contact.id) ?? null);
    setSelectedCompany(null);
    setContactEditing(false);
    setCompanyEditing(false);
    setSaveError("");
    routeToRecord("contact", contact.id);
  }

  function openCompany(company: ApiCompany) {
    setSelectedContact(null);
    setSelectedApiContact(null);
    setSelectedCompany(company);
    setContactEditing(false);
    setCompanyEditing(false);
    setSaveError("");
    routeToRecord("company", company.id);
  }

  function closeRecord() {
    const next = new URLSearchParams(routeSearchParams);
    next.delete("contact");
    next.delete("company");
    setRouteSearchParams(next);
    setSelectedContact(null);
    setSelectedApiContact(null);
    setSelectedCompany(null);
    setContactEditing(false);
    setCompanyEditing(false);
    setSaveError("");
  }

  function updateContactQueryCache(updated: ApiContact) {
    queryClient.setQueriesData<CursorPage<ApiContact>>({ queryKey: ["contacts"] }, (page) => page ? {
      ...page,
      items: page.items.map((contact) => contact.id === updated.id ? updated : contact),
    } : page);
  }

  function updateCompanyQueryCache(updated: ApiCompany) {
    queryClient.setQueriesData<CursorPage<ApiCompany>>({ queryKey: ["companies"] }, (page) => page ? {
      ...page,
      items: page.items.map((company) => company.id === updated.id ? updated : company),
    } : page);
    queryClient.setQueryData(["company-detail", updated.id], updated);
  }

  function removeContactFromQueryCache(contactId: string) {
    queryClient.setQueriesData<CursorPage<ApiContact>>({ queryKey: ["contacts"] }, (page) => page ? {
      ...page,
      items: page.items.filter((contact) => contact.id !== contactId),
    } : page);
  }

  function removeCompanyFromQueryCache(companyId: string) {
    queryClient.setQueriesData<CursorPage<ApiCompany>>({ queryKey: ["companies"] }, (page) => page ? {
      ...page,
      items: page.items.filter((company) => company.id !== companyId),
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
    const mutationGeneration = accessGeneration.current;
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
          ...(!isEmployee ? { assignee_id: String(data.get("assignee_id") ?? "") || null } : {}),
        });
        if (mutationGeneration !== accessGeneration.current) return;
        updateContactQueryCache(updated);
        setSelectedApiContact(updated);
        setSelectedContact(contactFromApi(updated, new Map(availableAssignees.map((user) => [user.id, user]))));
        await queryClient.invalidateQueries({ queryKey: ["contacts"] });
      } else {
        const updated: Contact = {
          ...selectedContact,
          name: `${firstName} ${lastName}`.trim(),
          email: email || "—",
          phone: phone || "—",
          tags,
          assignee: isEmployee
            ? selectedContact.assignee
            : availableAssignees.find((user) => user.id === String(data.get("assignee_id") ?? "")) ?? null,
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

  async function updateCompany(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedCompany || isEmployee) return;
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const email = String(data.get("email") ?? "").trim();
    const phone = String(data.get("phone") ?? "").trim();
    const website = String(data.get("website") ?? "").trim();
    const tags = parseDealTags(String(data.get("tags") ?? ""));
    const mutationGeneration = accessGeneration.current;
    setSaving(true);
    setSaveError("");
    try {
      if (remoteEnabled) {
        const updated = await api.patch<ApiCompany>(`/companies/${selectedCompany.id}`, {
          expected_version: selectedCompany.version,
          name,
          email: email || null,
          phone: phone || null,
          website: website || null,
          tags,
        });
        if (mutationGeneration !== accessGeneration.current) return;
        updateCompanyQueryCache(updated);
        setSelectedCompany(updated);
        await queryClient.invalidateQueries({ queryKey: ["companies"] });
      } else {
        const updated: ApiCompany = {
          ...selectedCompany,
          name,
          email: email || null,
          phone: phone || null,
          website: website || null,
          tags,
          version: selectedCompany.version + 1,
          updated_at: new Date().toISOString(),
        };
        setDemoCompanies((items) => items.map((company) => company.id === updated.id ? updated : company));
        setSelectedCompany(updated);
      }
      setCompanyEditing(false);
      setNotice("Компания обновлена");
    } catch {
      setSaveError("Не удалось обновить компанию. Возможно, запись уже была изменена.");
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelectedRecord() {
    const entity = selectedContact ?? selectedCompany;
    if (isEmployee || !entity) return;
    const entityKind = selectedContact ? "Контакт" : "Компания";
    const collection = selectedContact ? "contacts" : "companies";
    const version = selectedContact ? selectedApiContact?.version : selectedCompany?.version;
    setDeleting(true);
    setDeleteError("");
    try {
      if (remoteEnabled) {
        if (version == null) throw new Error("Record version is unavailable");
        await api.delete(`/${collection}/${entity.id}?expected_version=${version}`);
        if (selectedContact) removeContactFromQueryCache(entity.id);
        else removeCompanyFromQueryCache(entity.id);
        await queryClient.invalidateQueries({ queryKey: [collection] });
      } else if (selectedContact) {
        setDemoContacts((items) => items.filter((contact) => contact.id !== entity.id));
      } else {
        setDemoCompanies((items) => items.filter((company) => company.id !== entity.id));
      }
      setDeleteConfirmOpen(false);
      closeRecord();
      setNotice(`${entityKind} ${selectedContact ? "удалён" : "удалена"}`);
    } catch (reason) {
      setDeleteError(reason instanceof ApiError && reason.status === 404
        ? `${entityKind} уже ${selectedContact ? "удалён" : "удалена"} другим пользователем. Закройте карточку и обновите список.`
        : reason instanceof ApiError && reason.status === 409
          ? `${entityKind} была изменена другим пользователем. Закройте карточку, откройте её снова и повторите удаление.`
          : `Не удалось удалить ${selectedContact ? "контакт" : "компанию"}. Обновите страницу и попробуйте ещё раз.`);
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
      void Promise.all([remoteContacts.refetch(), remoteUsers.refetch()]);
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
        <Button className="contacts-page__desktop-add" variant="primary" aria-controls="new-client-dialog" onClick={() => setDialogOpen(true)}><Plus size={17} /> {isEmployee || view === "contacts" ? "Новый контакт" : "Новая компания"}</Button>
      </header>
      <div className="content-toolbar">
        <label className="search-control"><Search size={18} /><input value={search} onChange={(event) => updateSearch(event.target.value)} placeholder={isEmployee ? "Имя, тег, телефон или email" : "Имя, компания, тег, телефон или email"} /></label>
        {!isEmployee ? <div className="view-switch" aria-label="Тип клиентов"><button className={view === "contacts" ? "is-active" : ""} type="button" aria-label="Контакты" onClick={() => setView("contacts")}><Users size={18} /></button><button className={view === "companies" ? "is-active" : ""} type="button" aria-label="Компании" onClick={() => setView("companies")}><Building2 size={18} /></button></div> : null}
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
            <span className="data-row__primary"><span className="company-avatar">{contact.name.slice(0, 1)}</span><span><strong>{contact.name}</strong><small>{[...(!isEmployee && contact.company !== "—" ? [contact.company] : []), ...contact.tags].join(" · ") || "—"}</small></span></span>
            <span className="data-row__contact"><strong>{contact.phone}</strong><small>{contact.email}</small></span>
            <span>{remoteEnabled ? <><strong>—</strong><small>см. карточку</small></> : <><strong>{contact.deals}</strong><small>{formatMoney(contact.revenue)}</small></>}</span>
            <span>{contact.nextPurchaseAt ? formatLongDate(contact.nextPurchaseAt) : "Не запланирована"}</span>
            {contact.assignee ? <span className="owner-line"><Avatar user={contact.assignee} size="sm" /><span>{contact.assignee.name}</span></span> : <span className="owner-line owner-line--unassigned">Не назначен</span>}
          </button>
        ))}
      </section> : null}
      {!isEmployee && !loading && !failed && view === "companies" ? <section className="data-table" role="region" aria-label="Список компаний">
        <div className="data-table__header"><span>Компания</span><span>Контакты</span><span>Теги</span><span>Сайт</span><span>Обновлено</span></div>
        {visibleCompanies.map((company) => (
          <button className="data-row" type="button" key={company.id} onClick={() => openCompany(company)}>
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
        aria-label={isEmployee || view === "contacts" ? "Добавить контакт" : "Добавить компанию"}
        aria-controls="new-client-dialog"
        onClick={() => setDialogOpen(true)}
      >
        <Plus aria-hidden="true" size={24} />
      </button>

      <Dialog.Root open={dialogOpen} onOpenChange={setDialogOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay" />
          <Dialog.Content id="new-client-dialog" className="dialog-content">
            <div className="dialog-header"><div><Dialog.Title>{isEmployee || view === "contacts" ? "Новый контакт" : "Новая компания"}</Dialog.Title><Dialog.Description>Запись будет доступна всей команде.</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="Закрыть"><X size={20} /></Dialog.Close></div>
            <form className="form-stack" onSubmit={(event) => void createEntity(event)}>
              {view === "contacts" ? <><label className="field"><span>Имя</span><input name="first_name" required autoFocus /></label><label className="field"><span>Фамилия</span><input name="last_name" /></label></> : <label className="field"><span>Название</span><input name="name" required autoFocus /></label>}
              <label className="field"><span>Email</span><input name="email" type="email" /></label>
              <label className="field"><span>Телефон</span><input name="phone" type="tel" /></label>
              {view === "contacts" ? (isEmployee
                ? <div className="field" aria-label="Ответственный"><span>Ответственный</span><strong>{currentUser.name}</strong></div>
                : <label className="field"><span>Ответственный</span><select name="assignee_id" required defaultValue={currentUser.id}>{availableAssignees.map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}</select></label>) : null}
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
                  {!isEmployee ? <button type="button" className="icon-button record-delete-button" aria-label="Удалить контакт" title="Удалить контакт" onClick={() => { setDeleteError(""); setDeleteConfirmOpen(true); }}><Trash2 size={16} aria-hidden="true" /></button> : null}
                </> : null}
                {selectedCompany && !companyEditing && !isEmployee ? <>
                  <Button compact aria-label="Редактировать компанию" onClick={() => { setSaveError(""); setCompanyEditing(true); }}><Pencil size={15} /><span className="record-dialog__action-label">Редактировать</span></Button>
                  <button type="button" className="icon-button record-delete-button" aria-label="Удалить компанию" title="Удалить компанию" onClick={() => { setDeleteError(""); setDeleteConfirmOpen(true); }}><Trash2 size={16} aria-hidden="true" /></button>
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
              {isEmployee
                ? <div className="field" aria-label="Ответственный"><span>Ответственный</span><strong>{selectedContact.assignee?.name ?? currentUser.name}</strong></div>
                : <label className="field"><span>Ответственный</span><select name="assignee_id" defaultValue={selectedApiContact?.assignee_id ?? ""}><option value="">Не назначен</option>{availableAssignees.map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}</select></label>}
              {saveError ? <p className="form-error" role="alert">{saveError}</p> : null}
              <div className="dialog-actions"><Button type="button" onClick={() => { setSaveError(""); setContactEditing(false); }}>Отмена</Button><Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : "Сохранить"}</Button></div>
            </form> : null}
            {selectedCompany && companyEditing ? <form className="form-stack contact-edit-form" onSubmit={(event) => void updateCompany(event)}>
              <label className="field"><span>Название</span><input name="name" required autoFocus defaultValue={selectedCompany.name} /></label>
              <label className="field"><span>Email</span><input name="email" type="email" defaultValue={selectedCompany.email ?? ""} /></label>
              <label className="field"><span>Телефон</span><input name="phone" type="tel" defaultValue={selectedCompany.phone ?? ""} /></label>
              <label className="field"><span>Сайт</span><input name="website" type="url" defaultValue={selectedCompany.website ?? ""} placeholder="https://" /></label>
              <label className="field"><span>Теги</span><input name="tags" defaultValue={selectedCompany.tags.join(", ")} placeholder="Например: партнёр, поставщик" /></label>
              {saveError ? <p className="form-error" role="alert">{saveError}</p> : null}
              <div className="dialog-actions"><Button type="button" onClick={() => { setSaveError(""); setCompanyEditing(false); }}>Отмена</Button><Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : "Сохранить"}</Button></div>
            </form> : null}
            {selectedContact && !contactEditing ? <div className="record-detail-grid">
              <div><small>Телефон</small><strong>{selectedContact.phone}</strong></div>
              <div><small>Email</small><strong>{selectedContact.email}</strong></div>
              {!isEmployee ? <div><small>Компания</small>{selectedContactCompany
                ? <button className="record-entity-link" type="button" onClick={() => openCompany(selectedContactCompany)}><strong>{selectedContactCompany.name}</strong><ExternalLink size={14} aria-hidden="true" /></button>
                : <strong>{selectedContactCompanyQuery.isLoading ? "Загружаем…" : selectedContact.company}</strong>}</div> : null}
              <div><small>Ответственный</small><strong>{selectedContact.assignee?.name ?? "Не назначен"}</strong></div>
              <div className="record-detail-grid__wide"><small>Теги</small><strong>{selectedContact.tags.length ? selectedContact.tags.join(", ") : "Нет тегов"}</strong></div>
              <div><small>Покупок</small><strong>{remoteEnabled ? selectedPurchases.length : selectedContact.deals}</strong></div>
              <div><small>Покупок на сумму</small><strong>{formatMoney(remoteEnabled ? selectedRevenue : selectedContact.revenue)}</strong></div>
              <div className="record-detail-grid__wide"><small>Следующая покупка</small><strong>{selectedContact.nextPurchaseAt ? formatLongDate(selectedContact.nextPurchaseAt) : "Не запланирована"}</strong></div>
            </div> : null}
            {selectedCompany && !companyEditing ? <div className="record-detail-grid">
              <div><small>Телефон</small><strong>{selectedCompany.phone ?? "—"}</strong></div>
              <div><small>Email</small><strong>{selectedCompany.email ?? "—"}</strong></div>
              <div className="record-detail-grid__wide"><small>Сайт</small><strong>{selectedCompany.website ?? "Не указан"}</strong></div>
              <div><small>Теги</small><strong>{selectedCompany.tags.length ? selectedCompany.tags.join(", ") : "Нет тегов"}</strong></div>
              <div><small>Обновлено</small><strong>{formatLongDate(selectedCompany.updated_at)}</strong></div>
            </div> : null}
            {selectedContact && !contactEditing ? <section className="record-purchases">
              <h3>Сделки</h3>
              {contactDealsQuery.isLoading ? <p className="empty-copy">Загружаем сделки…</p> : null}
              {selectedContactDeals.map((deal) => <Link className="record-related-row" key={deal.id} to={`/deals?deal=${encodeURIComponent(deal.id)}`} aria-label={`Открыть сделку ${deal.title}`}><span><strong>{deal.title}</strong><small>{formatLongDate(deal.updated_at)}</small></span><span className="record-related-row__value"><b>{formatMoney(Number(deal.amount ?? 0))}</b><ExternalLink size={14} aria-hidden="true" /></span></Link>)}
              {!remoteEnabled || (!contactDealsQuery.isLoading && !selectedContactDeals.length) ? <p className="empty-copy">Связанные сделки появятся здесь.</p> : null}
            </section> : null}
            {selectedCompany && !companyEditing ? <section className="record-purchases">
              <h3>Контакты компании</h3>
              {companyContactsQuery.isLoading ? <p className="empty-copy">Загружаем контакты…</p> : null}
              {companyContacts.map((contact) => <button className="record-related-row" type="button" key={contact.id} onClick={() => openContact(contact, companyContactsQuery.data?.items.find((item) => item.id === contact.id) ?? null)} aria-label={`Открыть контакт ${contact.name}`}><span><strong>{contact.name}</strong><small>{[contact.phone, contact.email].filter((value) => value !== "—").join(" · ") || "Контакты не указаны"}</small></span><ExternalLink size={14} aria-hidden="true" /></button>)}
              {!companyContactsQuery.isLoading && !companyContacts.length ? <p className="empty-copy">У компании пока нет связанных контактов.</p> : null}
            </section> : null}
            {!contactEditing && !companyEditing ? <section className="record-timeline">
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

      {!isEmployee ? <Dialog.Root open={deleteConfirmOpen} onOpenChange={(open) => { if (!deleting) { setDeleteConfirmOpen(open); if (!open) setDeleteError(""); } }}>
        <Dialog.Portal>
          <Dialog.Overlay className="dialog-overlay dialog-overlay--confirm" />
          <Dialog.Content className="dialog-content task-delete-dialog confirm-dialog">
            <div className="dialog-header">
              <div><Dialog.Title>{selectedContact ? "Удалить контакт?" : "Удалить компанию?"}</Dialog.Title><Dialog.Description>Это действие нельзя отменить.</Dialog.Description></div>
              <Dialog.Close className="icon-button" aria-label="Закрыть подтверждение" disabled={deleting}><X size={20} /></Dialog.Close>
            </div>
            <p className="task-delete-dialog__copy">{selectedContact ? "Контакт" : "Компания"} <strong>«{selectedContact?.name ?? selectedCompany?.name}»</strong> будет {selectedContact ? "удалён" : "удалена"} без возможности восстановления.</p>
            {deleteError ? <p className="form-error" role="alert">{deleteError}</p> : null}
            <div className="dialog-actions"><Dialog.Close asChild><Button type="button" disabled={deleting}>Отмена</Button></Dialog.Close><Button type="button" variant="danger" disabled={deleting} onClick={() => void deleteSelectedRecord()}>{deleting ? "Удаляем…" : selectedContact ? "Удалить контакт" : "Удалить компанию"}</Button></div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root> : null}
    </div>
  );
}
