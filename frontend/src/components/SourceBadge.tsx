import { Braces, FileText, Mail, MessageCircle, MousePointerClick, PencilLine } from "lucide-react";

import type { SourceCode } from "../types/crm";

const icons = {
  manual: PencilLine,
  email: Mail,
  telegram: MessageCircle,
  max: MessageCircle,
  webhook: Braces,
  html_form: FileText,
  amo_import: MousePointerClick,
} satisfies Record<SourceCode, typeof Mail>;

interface SourceBadgeProps {
  source: SourceCode;
  label: string;
}

export function SourceBadge({ source, label }: SourceBadgeProps) {
  const Icon = icons[source];
  return (
    <span className={`source source--${source}`}>
      <Icon size={15} strokeWidth={1.8} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
