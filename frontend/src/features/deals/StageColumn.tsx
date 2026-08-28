import { useDroppable } from "@dnd-kit/core";
import { ChevronDown, Plus } from "lucide-react";
import { useState } from "react";

import { formatMoney } from "../../lib/format";
import type { Deal, Stage } from "../../types/crm";
import { DealCard } from "./DealCard";

interface StageColumnProps {
  stage: Stage;
  deals: Deal[];
  selectedDealId: string | null;
  onSelect: (dealId: string) => void;
  onAdd: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => Promise<void>;
}

export function StageColumn({ stage, deals, selectedDealId, onSelect, onAdd, hasMore, loadingMore, onLoadMore }: StageColumnProps) {
  const [expanded, setExpanded] = useState(true);
  const { setNodeRef, isOver } = useDroppable({ id: stage.id });
  const total = deals.reduce((sum, deal) => sum + deal.amount, 0);

  return (
    <section ref={setNodeRef} className={`stage stage--${stage.color}${isOver ? " stage--over" : ""}`}>
      <header className="stage__header">
        <button type="button" className="stage__summary" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
          <span className="stage__title">{stage.name}</span>
          <span className="stage__stats">{deals.length} сделки · {formatMoney(total)}</span>
        </button>
        <button type="button" className="stage__expand" onClick={() => setExpanded((value) => !value)} aria-label={expanded ? "Свернуть этап" : "Развернуть этап"}>
          <ChevronDown className={expanded ? "stage__chevron stage__chevron--up" : "stage__chevron"} size={18} />
        </button>
      </header>

      {expanded ? (
        <div className="stage__body">
          <div className="stage__cards">
            {deals.map((deal) => (
              <DealCard key={deal.id} deal={deal} selected={deal.id === selectedDealId} onSelect={onSelect} />
            ))}
          </div>
          <button type="button" className="stage__add" onClick={onAdd}>
            <Plus size={16} aria-hidden="true" />
            <span>Добавить сделку</span>
          </button>
          {hasMore ? <button type="button" className="stage__load-more" disabled={loadingMore} onClick={() => void onLoadMore()}>{loadingMore ? "Загружаем…" : "Показать ещё"}</button> : null}
        </div>
      ) : null}
    </section>
  );
}
