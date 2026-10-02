import * as Dialog from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { Download, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { api, ApiError, remoteEnabled } from "../../lib/api";
import type { Pipeline } from "../../types/crm";

export function DealExportControl({ pipeline, pipelines }: { pipeline: Pipeline; pipelines: Pipeline[] }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const downloadController = useRef<AbortController | null>(null);
  const policy = useQuery({
    queryKey: ["crm-export-policy"],
    queryFn: ({ signal }) => api.get<{ enabled: boolean }>("/admin/security/export-policy", { signal }),
    enabled: remoteEnabled && open,
  });
  useEffect(() => () => downloadController.current?.abort(), []);

  async function download(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const month = String(form.get("month"));
    const controller = new AbortController();
    downloadController.current = controller;
    setSaving(true);
    setError("");
    try {
      const blob = await api.postBlob("/deals/export", {
        month,
        date_field: form.get("date_field"),
        pipeline_id: form.get("pipeline_id") || null,
      }, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `deals-${month}.xlsx`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setOpen(false);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const detail = reason instanceof ApiError ? (reason.details as { detail?: { code?: string } })?.detail : undefined;
      setError(reason instanceof ApiError && reason.status === 403
        ? "Выгрузка отключена или у вас больше нет доступа."
        : detail?.code === "export_row_limit"
          ? "В месяце больше 10 000 сделок. Выберите отдельную воронку."
          : detail?.code === "export_cell_too_long"
            ? "Одно из полей превышает лимит Excel в 32 767 символов. Сократите его и повторите выгрузку."
            : "Не удалось выгрузить сделки. Попробуйте ещё раз.");
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  const now = new Date();
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  return <Dialog.Root open={open} onOpenChange={(next) => { if (!saving) { setError(""); setOpen(next); } }}>
    <Dialog.Trigger asChild><Button compact className="deal-export-button" aria-label="Выгрузить сделки в XLSX"><Download size={17} aria-hidden="true" /> XLSX</Button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="dialog-overlay" />
      <Dialog.Content className="dialog-content" onEscapeKeyDown={(event) => { if (saving) event.preventDefault(); }} onInteractOutside={(event) => { if (saving) event.preventDefault(); }}>
        <div className="dialog-header">
          <div><Dialog.Title>Выгрузка сделок</Dialog.Title><Dialog.Description>Таблица XLSX со всеми стандартными и пользовательскими полями. Даты учитывают часовой пояс рабочего пространства.</Dialog.Description></div>
          <Dialog.Close className="icon-button" aria-label="Закрыть выгрузку" disabled={saving}><X size={20} /></Dialog.Close>
        </div>
        <form className="form-stack" onSubmit={(event) => void download(event)}>
          <label className="field"><span>Месяц</span><input name="month" type="month" required defaultValue={month} disabled={saving} /></label>
          <label className="field"><span>Дата отбора</span><select name="date_field" defaultValue="created_at" disabled={saving}>
            <option value="created_at">Дата создания сделки</option><option value="next_purchase_at">Дата следующей покупки</option><option value="updated_at">Дата последнего изменения</option>
          </select></label>
          <label className="field"><span>Воронка</span><select name="pipeline_id" defaultValue={pipeline.id} disabled={saving}>
            <option value="">Все воронки</option>{pipelines.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
          <p className="empty-copy">Включены все этапы, в том числе успешные и закрытые сделки. Поиск и фильтр источника на странице не ограничивают выгрузку.</p>
          {!remoteEnabled ? <p role="status">Выгрузка доступна в подключённом рабочем пространстве.</p>
            : policy.isLoading ? <p role="status">Проверяем доступ…</p>
              : policy.isError ? <p className="form-error" role="alert">Не удалось проверить доступ. <button type="button" onClick={() => void policy.refetch()}>Повторить</button></p>
                : !policy.data?.enabled ? <p role="status">Выгрузка отключена администратором сервера.</p> : null}
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <div className="dialog-actions"><Dialog.Close asChild><Button type="button" disabled={saving}>Отмена</Button></Dialog.Close><Button type="submit" variant="primary" disabled={saving || !remoteEnabled || !policy.data?.enabled}>{saving ? "Готовим файл…" : "Скачать XLSX"}</Button></div>
        </form>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
