import { useDroppable } from "@dnd-kit/core";
import { Archive, ChevronDown, Plus } from "lucide-react";
import { useId, useState } from "react";

import { formatMoney } from "../../lib/format";
import type { Deal, Stage } from "../../types/crm";
import { DealCard } from "./DealCard";

interface StageColumnProps {
  stage: Stage;
  deals: Deal[];
  selectedDealId: string | null;
  onSelect: (dealId: string) => void;
  onAdd: () => void;
  deferred: boolean;
  loadError?: string | null;
  hasMore: boolean;
  loadingMore: boolean;
  requestsBusy?: boolean;
  onLoadDeferred: () => Promise<void>;
  onLoadMore: () => Promise<void>;
}

export function StageColumn({ stage, deals, selectedDealId, onSelect, onAdd, deferred, loadError, hasMore, loadingMore, requestsBusy = false, onLoadDeferred, onLoadMore }: StageColumnProps) {
  const [expanded, setExpanded] = useState(true);
  const titleId = useId();
  const bodyId = useId();
  const { setNodeRef, isOver } = useDroppable({ id: stage.id });
  const total = deals.reduce((sum, deal) => sum + deal.amount, 0);

  return (
    <section
      ref={setNodeRef}
      className={`stage stage--kanban-column stage--${stage.color}${isOver ? " stage--over" : ""}`}
      aria-labelledby={titleId}
    >
      <header className="stage__header">
        <button type="button" className="stage__summary" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded} aria-controls={bodyId}>
          <span id={titleId} className="stage__title">{stage.name}</span>
          {deferred
            ? <span className="stage__stats">Сделки по запросу</span>
            : <span className="stage__stats" aria-label={`${deals.length} ${dealCountLabel(deals.length)}, ${formatMoney(total)}`}>
              <span className="stage__count" aria-hidden="true">{deals.length} {dealCountLabel(deals.length)}</span>
              <span className="stage__stats-separator" aria-hidden="true"> · </span>
              <span className="stage__total" aria-hidden="true">{formatMoney(total)}</span>
            </span>}
        </button>
        <button type="button" className="stage__expand" onClick={() => setExpanded((value) => !value)} aria-label={expanded ? `Свернуть этап ${stage.name}` : `Развернуть этап ${stage.name}`} aria-expanded={expanded} aria-controls={bodyId}>
          <ChevronDown className={expanded ? "stage__chevron stage__chevron--up" : "stage__chevron"} size={18} aria-hidden="true" />
        </button>
      </header>

      <div id={bodyId} className="stage__body" hidden={!expanded}>
        {deferred ? <div className="stage__deferred">
          <Archive size={24} aria-hidden="true" />
          <p>Закрытые сделки не загружаются автоматически.</p>
          <button
            type="button"
            className="stage__load-deferred"
            disabled={requestsBusy}
            aria-label={`Загрузить этап «${stage.name}»`}
            onClick={() => void onLoadDeferred()}
          >{stage.name}</button>
          {loadingMore ? <small role="status">Загружаем сделки…</small> : null}
          {loadError ? <small className="stage__load-error" role="alert">{loadError}</small> : null}
        </div> : <>
          <div className="stage__cards">
            {deals.map((deal) => (
              <DealCard key={deal.id} deal={deal} selected={deal.id === selectedDealId} onSelect={onSelect} />
            ))}
          </div>
          <button type="button" className="stage__add" onClick={onAdd} aria-label={`Добавить сделку в этап ${stage.name}`}>
            <Plus size={16} aria-hidden="true" />
            <span>Добавить сделку</span>
          </button>
          {hasMore ? <button type="button" className="stage__load-more" disabled={requestsBusy} onClick={() => void onLoadMore()}>{loadingMore ? "Загружаем…" : "Показать ещё"}</button> : null}
        </>}
      </div>
    </section>
  );
}

function dealCountLabel(count: number) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return "сделок";
  if (mod10 === 1) return "сделка";
  if (mod10 >= 2 && mod10 <= 4) return "сделки";
  return "сделок";
}
