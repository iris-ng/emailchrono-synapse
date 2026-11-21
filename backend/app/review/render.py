"""Plain-text rendering of a review plan, for the terminal approver and logs."""

from __future__ import annotations

from typing import Any, Dict, List


def _card(email: Dict[str, Any]) -> str:
    return f"#{email['id']} {email.get('from') or '(no sender)'} | {email.get('date_utc') or 'undated'} | {email.get('first_line') or ''}"


def render_plan_text(plan: Dict[str, Any]) -> str:
    """Render a stored plan (from plans.get_plan) as plain text a person can check."""
    lines: List[str] = [
        f"Plan {plan['id']} ({plan['kind']}) for case {plan['case_id']}: {plan['status']}",
        f"Created {plan['created_at']}, expires {plan['expires_at']}",
        plan.get("summary") or "",
    ]
    for warning in plan.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    lines.append("")
    lines.append(f"Changes ({len(plan['items'])}):")
    for number, item in enumerate(plan["items"], start=1):
        if item.get("op") == "decide_duplicate_pair":
            if item["status"] == "duplicate":
                keep = item["email_a"] if item["email_a"]["id"] == item["keeper_id"] else item["email_b"]
                drop = item["email_b"] if keep is item["email_a"] else item["email_a"]
                lines.append(f" {number}. Duplicate. Keep #{keep['id']}, mark #{drop['id']} as its duplicate.")
                lines.append(f"      keep: {_card(keep)}")
                lines.append(f"      drop: {_card(drop)}")
            else:
                lines.append(f" {number}. Not a duplicate: #{item['email_a']['id']} and #{item['email_b']['id']}.")
                lines.append(f"      {_card(item['email_a'])}")
                lines.append(f"      {_card(item['email_b'])}")
            lines.append(f"      reasons: {', '.join(item.get('reason_labels') or item.get('reasons') or [])}")
            if item.get("evidence"):
                lines.append(f"      evidence: {'; '.join(item['evidence'])}")
            if item.get("agent_override"):
                lines.append(f"      NOTE: the agent chose this; the rule said {item.get('rule_decision')}.")
        else:
            lines.append(f" {number}. {item}")
    if plan.get("left_for_you"):
        lines.append("")
        lines.append(f"Left for you ({len(plan['left_for_you'])}), not changed by this plan:")
        for entry in plan["left_for_you"]:
            if "email_a" in entry:
                lines.append(f" - #{entry['email_a']['id']} and #{entry['email_b']['id']}: {entry.get('why', '')}")
            else:
                lines.append(f" - {entry}")
    return "\n".join(lines)
