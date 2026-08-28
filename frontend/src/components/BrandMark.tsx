interface BrandMarkProps {
  compact?: boolean;
}

export function BrandMark({ compact = false }: BrandMarkProps) {
  return (
    <div className="brand-mark" aria-label="Pulse CRM">
      <svg viewBox="0 0 44 28" aria-hidden="true">
        <path d="M2 15h7l3-8 5 17 5-22 5 23 4-13 3 6h8" />
      </svg>
      {compact ? null : <span>Pulse CRM</span>}
    </div>
  );
}
