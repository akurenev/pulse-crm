import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import type { SourceCode } from "../../types/crm";

interface NewDealDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: { title: string; subtitle: string; amount: number; source: SourceCode }) => Promise<void>;
}

export function NewDealDialog({ open, onOpenChange, onSubmit }: NewDealDialogProps) {
  const [saving, setSaving] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSaving(true);
    try {
      await onSubmit({
        title: String(form.get("title") ?? ""),
        subtitle: String(form.get("subtitle") ?? ""),
        amount: Number(form.get("amount") ?? 0),
        source: String(form.get("source") ?? "manual") as SourceCode,
      });
      onOpenChange(false);
      event.currentTarget.reset();
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
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
            <label className="field">
              <span>Потребность</span>
              <input name="subtitle" required placeholder="Кофе и расходные материалы" />
            </label>
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
