-- emailchrono-synapse: extra guards on review plans (from code review).
-- A finished plan (applied, declined, stale, expired) never changes status again,
-- so a late Decline or expiry cannot overwrite an applied plan.
CREATE TRIGGER IF NOT EXISTS review_plans_terminal_status
BEFORE UPDATE OF status ON review_plans
WHEN OLD.status IN ('applied', 'declined', 'stale', 'expired') AND NEW.status IS NOT OLD.status
BEGIN
  SELECT RAISE(ABORT, 'a finished review plan cannot change status');
END;

-- Once a person has approved a plan, the record of that approval is fixed.
CREATE TRIGGER IF NOT EXISTS review_plans_approval_frozen
BEFORE UPDATE OF approved_at, approved_via, approved_items_hash ON review_plans
WHEN OLD.approved_at IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'an approval cannot be changed once recorded');
END;
