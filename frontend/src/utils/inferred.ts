import type { EmailRecord, InferredFlag, InferredLabel } from "../types";

/** Words for each AI-inferred field, matching the backend .ecz Excel sheet. */
export const INFERRED_LABEL_TEXT: Record<InferredLabel, string> = {
  from: "from",
  to: "to",
  subject: "subject",
  date: "date",
  timezone: "time zone"
};

const FLAG_LABEL: Record<InferredFlag, InferredLabel> = {
  inferred_from: "from",
  inferred_to: "to",
  inferred_subject: "subject",
  inferred_date: "date",
  inferred_timezone: "timezone"
};

export const INFERRED_HEADING = "Inferred by AI review, not yet checked by a person.";
export const INFERRED_FOOTER = "Edit the field to confirm or fix it. That clears this mark.";

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

export function flagLabels(flags: InferredFlag[]): string[] {
  return flags.map((flag) => INFERRED_LABEL_TEXT[FLAG_LABEL[flag]] ?? flag);
}

/** The labels from `wanted` that are marked inferred on this email, in order. */
export function inferredLabels(email: EmailRecord, wanted: InferredLabel[]): InferredLabel[] {
  const marks = email.inferred ?? {};
  return wanted.filter((label) => label in marks);
}

/** Tooltip rows: each inferred field and its evidence. */
export function inferredItems(email: EmailRecord, labels: InferredLabel[]): { name: string; text: string }[] {
  const marks = email.inferred ?? {};
  return labels.map((label) => ({ name: capitalize(INFERRED_LABEL_TEXT[label]), text: marks[label] ?? "" }));
}

/** One cell for the Excel export, same format as the backend sheet: "to (evidence); time zone (evidence)". */
export function inferredExportCell(email: EmailRecord): string {
  return Object.entries(email.inferred ?? {})
    .map(([label, evidence]) => {
      const name = INFERRED_LABEL_TEXT[label as InferredLabel] ?? label;
      return evidence ? `${name} (${evidence})` : name;
    })
    .join("; ");
}
