import { useEffect, useRef, useState } from "react";
import { Bot, CheckCircle2, X } from "lucide-react";
import { getReviewPlan, listReviewPlans } from "../api/client";
import type { ReviewPlanDetail, ReviewPlanSummary } from "../types";

const POLL_MS = 4000;

const KIND_LABEL: Record<ReviewPlanSummary["kind"], string> = {
  duplicates: "Duplicate cleanup",
  chronology: "Chronology review"
};

export function describeSettledPlan(plan: ReviewPlanDetail): string {
  const label = `${KIND_LABEL[plan.kind]} plan ${plan.id}`;
  switch (plan.status) {
    case "applied":
      return `${label} was applied: ${plan.receipt?.item_count ?? plan.item_count} change(s), each in the audit trail.`;
    case "declined":
      return `${label} was declined. Nothing changed.`;
    case "expired":
      return `${label} expired before it was applied. Nothing changed.`;
    case "stale":
      return `${label} went stale because the case changed. Nothing changed; ask the agent for a new plan.`;
    default:
      return `${label} is ${plan.status}.`;
  }
}

/**
 * Shows AI review plans that are waiting for this case, checked every few
 * seconds and when the window regains focus. When a plan it was showing is
 * applied, declined or expires, it calls onSettled so the page can reload.
 */
export function ReviewPlanBanner({
  caseId,
  onOpen,
  onSettled
}: {
  caseId: number;
  onOpen: (planId: string) => void;
  onSettled: (plan: ReviewPlanDetail) => void;
}) {
  const [plans, setPlans] = useState<ReviewPlanSummary[]>([]);
  const [lastResult, setLastResult] = useState("");
  const seen = useRef<Set<string>>(new Set());
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;
    seen.current = new Set();

    async function poll() {
      try {
        const open = await listReviewPlans(caseId);
        if (cancelled) return;
        const openIds = new Set(open.map((plan) => plan.id));
        for (const id of [...seen.current]) {
          if (openIds.has(id)) continue;
          seen.current.delete(id);
          const detail = await getReviewPlan(id).catch(() => null);
          if (detail && !cancelled) {
            setLastResult(describeSettledPlan(detail));
            onSettledRef.current(detail);
          }
        }
        open.forEach((plan) => seen.current.add(plan.id));
        setPlans(open);
      } catch {
        // Keep showing the last known state; the next poll retries.
      }
    }

    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    const onFocus = () => void poll();
    window.addEventListener("focus", onFocus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [caseId]);

  if (plans.length === 0 && !lastResult) return null;

  return (
    <div className="review-banner-stack">
      {plans.map((plan) => (
        <div
          className={`review-banner ${plan.status === "approved" ? "is-approved" : ""}`}
          key={plan.id}
          role="status"
        >
          <Bot size={18} aria-hidden="true" />
          <div className="review-banner-text">
            <strong>
              {plan.status === "approved"
                ? `${KIND_LABEL[plan.kind]} approved. Waiting for the agent to apply it.`
                : `${KIND_LABEL[plan.kind]} plan waiting for your approval`}
            </strong>
            <span>
              {plan.item_count} change(s), {plan.left_for_you_count} left for you. Plan {plan.id}.
            </span>
          </div>
          <button className="review-banner-button" type="button" onClick={() => onOpen(plan.id)}>
            {plan.status === "approved" ? "View plan" : "Review plan"}
          </button>
        </div>
      ))}
      {lastResult && (
        <div className="review-banner is-result" role="status">
          <CheckCircle2 size={18} aria-hidden="true" />
          <div className="review-banner-text">
            <span>{lastResult}</span>
          </div>
          <button
            className="icon-button"
            type="button"
            title="Dismiss"
            aria-label="Dismiss"
            onClick={() => setLastResult("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
    </div>
  );
}
