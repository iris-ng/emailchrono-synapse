import { Fragment, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, X } from "lucide-react";
import { approveReviewPlan, declineReviewPlan, getReviewPlan } from "../api/client";
import type {
  ChronologyCardSnapshot,
  ChronologyFieldChange,
  ChronologyPartFields,
  DuplicatePlanItem,
  InferredFlag,
  LeftForYouEntry,
  ReviewCardSnapshot,
  ReviewPlanDetail,
  ReviewPlanItem,
  SetFieldsPlanItem,
  SplitPlanItem
} from "../types";
import { flagLabels } from "../utils/inferred";
import { InferredMark } from "./InferredMark";

const KIND_TITLE = { duplicates: "Duplicate cleanup", chronology: "Chronology review" } as const;
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function formatWhen(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return "Undated";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone }).format(date);
  } catch {
    return date.toISOString();
  }
}

function minutesBetween(fromIso: string, to: number): number {
  return Math.round((to - new Date(fromIso).getTime()) / 60000);
}

function parseError(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  try {
    const body = JSON.parse(text);
    return body?.detail?.message ?? body?.detail ?? text;
  } catch {
    return text;
  }
}

/**
 * Renders a stored review plan from the database (never from the model's
 * text) so the person approves exactly what will be applied.
 */
export function ReviewPlanPanel({
  planId,
  timeZone,
  onClose,
  onDecided
}: {
  planId: string | null;
  timeZone?: string;
  onClose: () => void;
  onDecided: (plan: ReviewPlanDetail) => void;
}) {
  const [plan, setPlan] = useState<ReviewPlanDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const panelRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Modal behaviour: focus moves into the panel, Tab stays inside, Escape closes
  // (closing never decides the plan), and focus goes back where it came from.
  useEffect(() => {
    if (!planId) return;
    const returnTo = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      const panel = panelRef.current;
      if (!panel) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
        (el) => !el.hasAttribute("disabled") && el.offsetParent !== null
      );
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (returnTo && document.contains(returnTo)) returnTo.focus();
    };
  }, [planId]);

  useEffect(() => {
    if (!planId) return;
    let cancelled = false;
    setPlan(null);
    setError("");
    const load = () =>
      getReviewPlan(planId)
        .then((loaded) => !cancelled && setPlan(loaded))
        .catch((err) => !cancelled && setError(parseError(err)));
    void load();
    // Re-check every 10 s so a plan that goes stale or expires is shown as such.
    const timer = window.setInterval(() => {
      setNow(Date.now());
      void load();
    }, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [planId]);

  if (!planId) return null;

  async function decide(action: "approve" | "decline") {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      const updated =
        action === "approve"
          ? await approveReviewPlan(plan.id, plan.items_hash)
          : await declineReviewPlan(plan.id);
      setPlan(updated);
      onDecided(updated);
    } catch (err) {
      setError(parseError(err));
      getReviewPlan(plan.id).then(setPlan).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  const waiting = plan?.status === "planned";
  const expiresIn = plan ? -minutesBetween(plan.expires_at, now) : 0;

  return (
    <div
      className="duplicate-dialog-scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={panelRef}
        className="review-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Review AI plan"
        tabIndex={-1}
      >
        <header>
          <div>
            <p className="eyebrow">AI review plan</p>
            <h2>{plan ? KIND_TITLE[plan.kind] : "Loading plan..."}</h2>
            {plan && (
              <span>
                Plan {plan.id}. Created {Math.max(0, minutesBetween(plan.created_at, now))} min ago
                {waiting ? `, expires in ${Math.max(0, expiresIn)} min` : ""}.{" "}
                <b className={`review-status is-${plan.status}`}>{plan.status}</b>
              </span>
            )}
          </div>
          <button className="icon-button" type="button" title="Close" onClick={onClose}>
            <X size={17} />
          </button>
        </header>

        <div className="review-panel-body">
          {error && <div className="notice error">{error}</div>}
          {plan?.stale_now && (
            <div className="review-warning">
              <AlertTriangle size={16} aria-hidden="true" />
              The case changed after this plan was built. It cannot be approved; ask the agent for a new plan.
            </div>
          )}
          {plan?.warnings.map((warning) => (
            <div className="review-warning is-soft" key={warning}>
              <AlertTriangle size={16} aria-hidden="true" />
              {warning}
            </div>
          ))}
          {plan && (
            <>
              <h3 className="review-section-title">Changes ({plan.items.length})</h3>
              <ol className="review-items">
                {plan.items.map((item, index) => (
                  <li key={index}>
                    <PlanItem item={item} timeZone={timeZone} />
                  </li>
                ))}
              </ol>
              {plan.left_for_you.length > 0 && (
                <>
                  <h3 className="review-section-title">
                    Left for you ({plan.left_for_you.length}). This plan does not change these.
                  </h3>
                  <ul className="review-items is-left">
                    {plan.left_for_you.map((entry, index) => (
                      <li key={index}>
                        <LeftItem entry={entry} timeZone={timeZone} />
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>

        <footer className="review-panel-footer">
          {plan && waiting ? (
            <>
              <span>Approve applies every change above in one step. Nothing changes until then.</span>
              <button className="review-decline" type="button" disabled={busy} onClick={() => void decide("decline")}>
                Decline
              </button>
              <button
                className="review-approve"
                type="button"
                disabled={busy || plan.stale_now}
                onClick={() => void decide("approve")}
              >
                <Check size={15} /> Approve
              </button>
            </>
          ) : (
            <span>
              {plan?.status === "approved" && "Approved. The agent will apply it next."}
              {plan?.status === "applied" && `Applied ${formatWhen(plan.applied_at, timeZone)}.`}
              {plan?.status === "declined" && "Declined. Nothing changed."}
              {plan?.status === "stale" && "Stale. Nothing changed."}
              {plan?.status === "expired" && "Expired. Nothing changed."}
            </span>
          )}
        </footer>
      </section>
    </div>
  );
}

function Card({ card, label, tone, timeZone }: { card: ReviewCardSnapshot; label: string; tone?: string; timeZone?: string }) {
  return (
    <div className={`review-card ${tone ?? ""}`}>
      <span className="review-card-label">
        {label} <small>#{card.id}</small>
      </span>
      <strong>{card.from || "Unknown sender"}</strong>
      <span>{formatWhen(card.date_utc, timeZone)}</span>
      <span className="review-card-subject">{card.subject || "(no subject)"}</span>
      <q>{card.first_line}</q>
    </div>
  );
}

function PlanItem({ item, timeZone }: { item: ReviewPlanItem; timeZone?: string }) {
  if (item.op === "decide_duplicate_pair") {
    const dup = item as unknown as DuplicatePlanItem;
    const isDuplicate = dup.status === "duplicate";
    const keep = dup.email_a.id === dup.keeper_id ? dup.email_a : dup.email_b;
    const drop = keep === dup.email_a ? dup.email_b : dup.email_a;
    return (
      <div className="review-item">
        <p className="review-item-title">
          {isDuplicate ? `Duplicate: keep #${keep.id}, mark #${drop.id} as its copy` : `Not a duplicate: #${dup.email_a.id} and #${dup.email_b.id}`}
        </p>
        <div className="review-compare">
          {isDuplicate ? (
            <>
              <Card card={keep} label="Keep" tone="is-keep" timeZone={timeZone} />
              <Card card={drop} label="Duplicate" tone="is-drop" timeZone={timeZone} />
            </>
          ) : (
            <>
              <Card card={dup.email_a} label="Email" timeZone={timeZone} />
              <Card card={dup.email_b} label="Different email" timeZone={timeZone} />
            </>
          )}
        </div>
        <p className="review-reasons">Matched on: {dup.reason_labels.join(", ")}</p>
        {dup.evidence.length > 0 && (
          <ul className="review-evidence">
            {dup.evidence.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {dup.agent_override && (
          <p className="review-override">
            <AlertTriangle size={14} aria-hidden="true" /> The agent chose this. The rule suggested "{dup.rule_decision}".
          </p>
        )}
      </div>
    );
  }
  return <ChronologyItem item={item} timeZone={timeZone} />;
}

const PROBLEM_TEXT: Record<string, string> = {
  merged: "Holds more than one email",
  undated: "No date",
  chain_date_conflict: "Date out of order",
  low_confidence: "Low parse confidence",
  bad_sender: "Bad sender",
  ai_error: "AI review failed"
};

const FIELD_TEXT: Record<ChronologyFieldChange["field"], string> = {
  from_addr: "From",
  to: "To",
  subject: "Subject",
  date_utc: "Date"
};

function Problems({ codes }: { codes: string[] }) {
  if (codes.length === 0) return null;
  return (
    <span className="review-problems">
      {codes.map((code) => (
        <span className="review-problem" key={code}>
          {PROBLEM_TEXT[code] ?? code}
        </span>
      ))}
    </span>
  );
}

function InferredTag({ flags }: { flags: InferredFlag[] }) {
  if (flags.length === 0) return null;
  const what = flagLabels(flags).join(" and ");
  return (
    <InferredMark
      label={`inferred ${what}`}
      heading={`The AI inferred the ${what}. The evidence line shows where it came from.`}
      footer='After apply, the card shows an "inferred" mark until a person edits the field.'
    />
  );
}

function fieldValue(field: ChronologyFieldChange["field"] | "date_raw" | "first_line", value: unknown, timeZone?: string): string {
  if (Array.isArray(value)) return value.length ? value.join("; ") : "(none)";
  if (value === null || value === undefined || value === "") return field === "date_utc" ? "Undated" : "(empty)";
  if (field === "date_utc") return formatWhen(String(value), timeZone);
  return String(value);
}

/** One AI change: field, old to new, the evidence, and an inferred tag. */
function FieldChange({ change, timeZone }: { change: ChronologyFieldChange; timeZone?: string }) {
  return (
    <li className="review-change">
      <div className="review-change-head">
        <b>{FIELD_TEXT[change.field] ?? change.field}</b>
        <s>{fieldValue(change.field, change.old, timeZone)}</s>
        <span aria-hidden="true">→</span>
        <strong>{fieldValue(change.field, change.new, timeZone)}</strong>
        <InferredTag flags={change.inferred} />
      </div>
      {change.quote && change.field === "date_utc" && <span className="review-change-quote">Text says: "{change.quote}"</span>}
      <span className="review-change-evidence">Evidence: {change.evidence}</span>
    </li>
  );
}

function Rejected({ rejected }: { rejected: { field: string; value: unknown; why: string }[] }) {
  if (rejected.length === 0) return null;
  return (
    <ul className="review-rejected">
      {rejected.map((entry, index) => (
        <li key={index}>
          Not used for {FIELD_TEXT[entry.field as ChronologyFieldChange["field"]] ?? entry.field}:{" "}
          <q>{Array.isArray(entry.value) ? entry.value.join("; ") : String(entry.value ?? "")}</q>. {entry.why}
        </li>
      ))}
    </ul>
  );
}

/** A card's fields as they will be after apply; AI-changed fields are highlighted. */
function FieldsAfter({
  after,
  changes,
  timeZone
}: {
  after: ChronologyPartFields;
  changes: ChronologyFieldChange[];
  timeZone?: string;
}) {
  const changed = new Set(changes.map((change) => change.field));
  const rows: { field: ChronologyFieldChange["field"]; value: unknown }[] = [
    { field: "from_addr", value: after.from_addr },
    { field: "to", value: after.to },
    { field: "subject", value: after.subject },
    { field: "date_utc", value: after.date_utc }
  ];
  return (
    <dl className="review-fields">
      {rows.map(({ field, value }) => (
        <Fragment key={field}>
          <dt>{FIELD_TEXT[field]}</dt>
          <dd className={changed.has(field) ? "is-changed" : undefined}>
            {fieldValue(field, value, timeZone)}
            {field === "date_utc" && after.date_raw && <small> ("{after.date_raw}")</small>}
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}

function ChronologyCard({ card, timeZone }: { card: ChronologyCardSnapshot; timeZone?: string }) {
  return (
    <div className="review-card">
      <span className="review-card-label">
        Now <small>#{card.id}</small>
      </span>
      <strong>{card.from || "Unknown sender"}</strong>
      {card.to?.length > 0 && <span>To {card.to.join("; ")}</span>}
      <span>
        {formatWhen(card.date_utc, timeZone)}
        {card.date_raw && <small> ("{card.date_raw}")</small>}
      </span>
      <span className="review-card-subject">{card.subject || "(no subject)"}</span>
      <q>{card.first_line}</q>
    </div>
  );
}

function SplitItem({ item, timeZone }: { item: SplitPlanItem; timeZone?: string }) {
  return (
    <div className="review-item">
      <p className="review-item-title">
        Split #{item.email_id} into {item.parts.length} emails <Problems codes={item.problems} />
      </p>
      <ChronologyCard card={item.card} timeZone={timeZone} />
      <p className="review-reasons">Cut before {item.split_lines.length === 1 ? "this line" : "these lines"}:</p>
      <ul className="review-evidence review-split-lines">
        {item.split_lines.map((line, index) => (
          <li key={index}>
            <code>{line}</code>
          </li>
        ))}
      </ul>
      <div className="review-parts">
        {item.parts.map((part) => (
          <div className="review-card review-part" key={part.part_index}>
            <span className="review-card-label">
              Part {part.part_index} {part.part_index === 1 ? `(new card, keeps #${item.email_id}'s notes)` : "(new card)"}
            </span>
            <FieldsAfter after={part.after} changes={part.changes} timeZone={timeZone} />
            <q>{part.after.first_line || "(no text)"}</q>
            {part.changes.length > 0 && (
              <ul className="review-changes">
                {part.changes.map((change) => (
                  <FieldChange change={change} timeZone={timeZone} key={change.field} />
                ))}
              </ul>
            )}
            <Rejected rejected={part.rejected} />
            <details className="review-part-text">
              <summary>Show this part's text</summary>
              <pre>{part.after.body_text}</pre>
            </details>
          </div>
        ))}
      </div>
    </div>
  );
}

function SetFieldsItem({ item, timeZone }: { item: SetFieldsPlanItem; timeZone?: string }) {
  return (
    <div className="review-item">
      <p className="review-item-title">
        Fix {item.changes.length === 1 ? "1 field" : `${item.changes.length} fields`} on #{item.email_id}{" "}
        <Problems codes={item.problems} />
      </p>
      <div className="review-compare">
        <ChronologyCard card={item.card} timeZone={timeZone} />
        <div className="review-card is-keep">
          <span className="review-card-label">After</span>
          <FieldsAfter after={item.after} changes={item.changes} timeZone={timeZone} />
        </div>
      </div>
      <ul className="review-changes">
        {item.changes.map((change) => (
          <FieldChange change={change} timeZone={timeZone} key={change.field} />
        ))}
      </ul>
      <Rejected rejected={item.rejected} />
    </div>
  );
}

function ChronologyItem({ item, timeZone }: { item: ReviewPlanItem; timeZone?: string }) {
  if (item.op === "split") return <SplitItem item={item as unknown as SplitPlanItem} timeZone={timeZone} />;
  if (item.op === "set_fields") return <SetFieldsItem item={item as unknown as SetFieldsPlanItem} timeZone={timeZone} />;
  // Unknown item type: show it raw rather than hide it from the approver.
  return (
    <div className="review-item">
      <p className="review-item-title">{item.op}</p>
      <pre className="review-raw">{JSON.stringify(item, null, 2)}</pre>
    </div>
  );
}

function LeftItem({ entry, timeZone }: { entry: LeftForYouEntry; timeZone?: string }) {
  return (
    <div className="review-item">
      {entry.problem && (
        <p className="review-item-title">
          <Problems codes={[entry.problem]} />
          {entry.part_index ? ` Part ${entry.part_index}` : ""}
        </p>
      )}
      {entry.email_a && entry.email_b ? (
        <div className="review-compare">
          <Card card={entry.email_a} label="Email" timeZone={timeZone} />
          <Card card={entry.email_b} label="Email" timeZone={timeZone} />
        </div>
      ) : entry.email ? (
        <Card card={entry.email} label="Card" timeZone={timeZone} />
      ) : null}
      {entry.why && <p className="review-reasons">{entry.why}</p>}
    </div>
  );
}
