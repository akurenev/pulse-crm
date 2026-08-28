import { CSS } from "@dnd-kit/utilities";
import { useDraggable } from "@dnd-kit/core";
import { GripVertical } from "lucide-react";

import { Avatar } from "../../components/Avatar";
import { SourceBadge } from "../../components/SourceBadge";
import { formatMoney, formatShortDate } from "../../lib/format";
import type { Deal } from "../../types/crm";

interface DealCardProps {
  deal: Deal;
  selected: boolean;
  onSelect: (dealId: string) => void;
}

export function DealCard({ deal, selected, onSelect }: DealCardProps) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: deal.id,
    data: { stageId: deal.stageId },
  });

  return (
    <article
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform) }}
      className={`deal-card${selected ? " deal-card--selected" : ""}${isDragging ? " deal-card--dragging" : ""}`}
    >
      <button
        type="button"
        className="deal-card__drag"
        aria-label={`Переместить сделку ${deal.title}`}
        {...attributes}
        {...listeners}
      >
        <GripVertical size={16} aria-hidden="true" />
      </button>
      <button type="button" className="deal-card__content" onClick={() => onSelect(deal.id)}>
        <span className="deal-card__title">{deal.title}</span>
        <span className="deal-card__subtitle">{deal.subtitle}</span>
        <strong className="deal-card__amount">{formatMoney(deal.amount)}</strong>
        <span className="deal-card__meta">
          <SourceBadge source={deal.source} label={deal.sourceLabel} />
          <span className="deal-card__owner">
            <Avatar user={deal.assignee} size="sm" />
            <time dateTime={deal.dueDate}>{formatShortDate(deal.dueDate)}</time>
          </span>
        </span>
      </button>
    </article>
  );
}
