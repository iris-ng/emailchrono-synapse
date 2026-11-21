import { useEffect, useState } from "react";
import { AlertTriangle, Check, X } from "lucide-react";
import { approveReviewPlan, declineReviewPlan, getReviewPlan } from "../api/client";
import type {
  DuplicatePlanItem,
  LeftForYouEntry,
  ReviewCardSnapshot,
  ReviewPlanDetail,
  ReviewPlanItem
} from "../types";

const KIND_TITLE = { duplicates: "Duplicate cleanup", chronology: "Chronology review" } as const;

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
      <section className="review-panel" role="dialog" aria-modal="true" aria-label="Review AI plan">
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

/** Fallback for item types without a dedicated view yet. */
function ChronologyItem({ item }: { item: ReviewPlanItem; timeZone?: string }) {
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
