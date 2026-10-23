import { Plus, Trash2 } from "lucide-react";
import { useId, useRef, useState } from "react";

import { MAX_CONTACT_POINTS, type ContactPointKind } from "../lib/contact-points";
import { Button } from "./Button";

interface ContactPointFieldsProps {
  kind: ContactPointKind;
  defaultValues?: string[];
}

interface ContactPointRow {
  id: number;
  defaultValue: string;
  autoFocus?: boolean;
}

export function ContactPointFields({ kind, defaultValues }: ContactPointFieldsProps) {
  const fieldId = useId();
  const [rows, setRows] = useState<ContactPointRow[]>(() =>
    (defaultValues?.length ? defaultValues : [""]).map((defaultValue, id) => ({ id, defaultValue })),
  );
  const nextId = useRef(rows.length);
  const inputs = useRef(new Map<number, HTMLInputElement>());
  const label = kind === "email" ? "Email" : "Телефон";
  const itemLabel = kind === "email" ? "email" : "телефон";
  const limitLabel = kind === "email" ? "До 20 адресов email." : "До 20 телефонов.";
  const limitReached = rows.length >= MAX_CONTACT_POINTS;

  function addRow() {
    if (limitReached) return;
    const row = { id: nextId.current++, defaultValue: "", autoFocus: true };
    setRows((current) => current.length >= MAX_CONTACT_POINTS ? current : [...current, row]);
  }

  function removeRow(id: number) {
    if (rows.length === 1) {
      const input = inputs.current.get(id);
      if (input) {
        input.value = "";
        input.focus();
      }
      return;
    }

    const index = rows.findIndex((row) => row.id === id);
    const remaining = rows.filter((row) => row.id !== id);
    setRows(remaining);
    inputs.current.get(remaining[Math.min(index, remaining.length - 1)].id)?.focus();
  }

  return (
    <fieldset className="contact-point-fields field" aria-describedby={`${fieldId}-hint`}>
      <legend>{kind === "email" ? "Email" : "Телефоны"}</legend>
      <div className="contact-point-fields__rows">
        {rows.map((row, index) => (
          <div className="contact-point-fields__row" key={row.id}>
            <label className="sr-only" htmlFor={`${fieldId}-${row.id}`}>{index === 0 ? label : `${label} ${index + 1}`}</label>
            <input
              ref={(input) => {
                if (input) inputs.current.set(row.id, input);
                else inputs.current.delete(row.id);
              }}
              id={`${fieldId}-${row.id}`}
              name={kind}
              type={kind === "email" ? "email" : "tel"}
              maxLength={kind === "email" ? 320 : 64}
              defaultValue={row.defaultValue}
              autoFocus={row.autoFocus}
              aria-describedby={`${fieldId}-hint`}
            />
            <button
              className="icon-button contact-point-fields__remove"
              type="button"
              aria-label={`Удалить ${itemLabel} ${index + 1}`}
              onClick={() => removeRow(row.id)}
            >
              <Trash2 size={16} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
      <Button
        className="contact-point-fields__add"
        type="button"
        compact
        disabled={limitReached}
        onClick={addRow}
      >
        <Plus size={14} aria-hidden="true" />Добавить {itemLabel}
      </Button>
      <small id={`${fieldId}-hint`} className="contact-point-fields__hint">
        Первое заполненное значение — основное. {limitLabel}
      </small>
    </fieldset>
  );
}
