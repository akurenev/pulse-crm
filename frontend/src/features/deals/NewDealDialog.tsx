import * as Dialog from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useDeferredValue, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { api, remoteEnabled } from "../../lib/api";
import type { ApiCompany, ApiContact, CursorPage } from "../../types/api";
import type { SourceCode } from "../../types/crm";

interface NewDealLink {
  id?: string;
  name: string;
  phone?: string;
  email?: string;
}

interface NewDealDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: { title: string; amount: number; source: SourceCode; company?: NewDealLink; contact?: NewDealLink }) => Promise<void>;
}

export function NewDealDialog({ open, onOpenChange, onSubmit }: NewDealDialogProps) {
  const [saving, setSaving] = useState(false);
  const [companySearch, setCompanySearch] = useState("");
  const [contactSearch, setContactSearch] = useState("");
  const [company, setCompany] = useState<NewDealLink | null>(null);
  const [contact, setContact] = useState<NewDealLink | null>(null);
  const deferredCompanySearch = useDeferredValue(companySearch.trim());
  const deferredContactSearch = useDeferredValue(contactSearch.trim());
  const companiesQuery = useQuery({
    queryKey: ["new-deal-company", deferredCompanySearch],
    queryFn: ({ signal }) => api.get<CursorPage<ApiCompany>>(`/companies?limit=10&search=${encodeURIComponent(deferredCompanySearch)}`, { signal }),
    enabled: remoteEnabled && open && !company && deferredCompanySearch.length >= 2,
  });
  const contactsQuery = useQuery({
    queryKey: ["new-deal-contact", deferredContactSearch],
    queryFn: ({ signal }) => api.get<CursorPage<ApiContact>>(`/contacts?limit=10&search=${encodeURIComponent(deferredContactSearch)}`, { signal }),
    enabled: remoteEnabled && open && !contact && deferredContactSearch.length >= 2,
  });

  function resetLinks() {
    setCompanySearch("");
    setContactSearch("");
    setCompany(null);
    setContact(null);
  }

  function close(openState: boolean) {
    if (!openState && !saving) resetLinks();
    onOpenChange(openState);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setSaving(true);
    try {
      await onSubmit({
        title: String(form.get("title") ?? ""),
        amount: Number(form.get("amount") ?? 0),
        source: String(form.get("source") ?? "manual") as SourceCode,
        company: company ?? (companySearch.trim() ? { name: companySearch.trim() } : undefined),
        contact: contact ?? (contactSearch.trim() ? { name: contactSearch.trim() } : undefined),
      });
      onOpenChange(false);
      formElement.reset();
      resetLinks();
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={close}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <div className="dialog-header">
            <div>
              <Dialog.Title>Новая сделка</Dialog.Title>
              <Dialog.Description>Создайте карточку на первом этапе выбранной воронки.</Dialog.Description>
            </div>
            <Dialog.Close className="icon-button" aria-label="Закрыть">
              <X size={20} />
            </Dialog.Close>
          </div>
          <form className="form-stack" onSubmit={handleSubmit}>
            <label className="field">
              <span>Название</span>
              <input name="title" required autoFocus placeholder="Например, Кофейня Север" />
            </label>
            <DealLinkPicker
              label="Организация"
              placeholder="Введите название организации"
              search={companySearch}
              selected={company}
              loading={companiesQuery.isLoading}
              results={companiesQuery.data?.items ?? []}
              onSearchChange={(value) => { setCompanySearch(value); setCompany(null); }}
              onSelect={(selected) => { setCompany({ id: selected.id, name: selected.name }); setCompanySearch(selected.name); }}
              onClear={() => { setCompany(null); setCompanySearch(""); }}
            />
            <DealLinkPicker
              label="Контакт"
              placeholder="Введите имя, телефон или email"
              search={contactSearch}
              selected={contact}
              loading={contactsQuery.isLoading}
              results={contactsQuery.data?.items ?? []}
              onSearchChange={(value) => { setContactSearch(value); setContact(null); }}
              onSelect={(selected) => {
                const name = `${selected.first_name} ${selected.last_name}`.trim();
                setContact({ id: selected.id, name, phone: selected.primary_phone ?? selected.phones[0] ?? undefined, email: selected.primary_email ?? selected.emails[0] ?? undefined });
                setContactSearch(name);
              }}
              onClear={() => { setContact(null); setContactSearch(""); }}
            />
            <label className="field">
              <span>Сумма, ₽</span>
              <input name="amount" type="number" min="0" step="100" required placeholder="50000" />
            </label>
            <label className="field">
              <span>Источник</span>
              <select name="source" defaultValue="manual">
                <option value="manual">Ручной ввод</option>
                <option value="email">Email</option>
                <option value="telegram">Telegram</option>
                <option value="max">MAX</option>
                <option value="webhook">Webhook</option>
                <option value="html_form">HTML-форма</option>
              </select>
            </label>
            <div className="dialog-actions">
              <Dialog.Close asChild>
                <Button type="button">Отмена</Button>
              </Dialog.Close>
              <Button type="submit" variant="primary" disabled={saving}>{saving ? "Сохраняем…" : "Создать сделку"}</Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function DealLinkPicker({
  label,
  placeholder,
  search,
  selected,
  loading,
  results,
  onSearchChange,
  onSelect,
  onClear,
}: {
  label: string;
  placeholder: string;
  search: string;
  selected: NewDealLink | null;
  loading: boolean;
  results: ApiCompany[] | ApiContact[];
  onSearchChange: (value: string) => void;
  onSelect: (item: ApiCompany & ApiContact) => void;
  onClear: () => void;
}) {
  const canSearch = remoteEnabled && !selected && search.trim().length >= 2;
  return <label className="field new-deal-link-picker">
    <span>{label}</span>
    <span className="new-deal-link-picker__input">
      <input aria-label={label} value={search} placeholder={placeholder} required onChange={(event) => onSearchChange(event.target.value)} />
      {selected ? <button type="button" aria-label={`Очистить поле ${label}`} onClick={onClear}>×</button> : null}
    </span>
    {loading ? <small>Ищем…</small> : null}
    {canSearch && !loading ? <span className="new-deal-link-picker__results">
      {results.map((item) => {
        const isContact = "first_name" in item;
        const name = isContact ? `${item.first_name} ${item.last_name}`.trim() : item.name;
        const detail = isContact ? item.primary_phone ?? item.primary_email ?? "Без контактов" : item.inn ?? item.phone ?? item.email ?? "Без реквизитов";
        return <button type="button" key={item.id} onClick={() => onSelect(item as ApiCompany & ApiContact)}><strong>{name}</strong><small>{detail}</small></button>;
      })}
      {!results.length ? <small>Ничего не найдено — можно сохранить введённое название.</small> : null}
    </span> : null}
  </label>;
}
