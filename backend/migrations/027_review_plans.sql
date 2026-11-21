-- emailchrono-synapse: review plans for the AI review tools.
-- A plan is built by a tool, approved or declined by a person in the web app
-- (or the backup terminal script), and only then applied, all in one transaction.
CREATE TABLE IF NOT EXISTS review_plans (
  id TEXT PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('chronology', 'duplicates')),
  status TEXT NOT NULL DEFAULT 'planned'
    CHECK (status IN ('planned', 'approved', 'applied', 'declined', 'stale', 'expired')),
  summary TEXT NOT NULL DEFAULT '',
  items_json TEXT NOT NULL,
  left_for_you_json TEXT NOT NULL DEFAULT '[]',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  touched_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  items_hash TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  approved_at TEXT,
  approved_via TEXT CHECK (approved_via IS NULL OR approved_via IN ('web', 'terminal', 'bench')),
  approved_items_hash TEXT,
  declined_at TEXT,
  declined_via TEXT,
  applied_at TEXT,
  receipt_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_plans_case_status ON review_plans(case_id, status);

-- Plans are never edited after creation: the person approves exactly what was shown.
CREATE TRIGGER IF NOT EXISTS review_plans_frozen_content
BEFORE UPDATE OF case_id, kind, summary, items_json, left_for_you_json, warnings_json,
                 touched_json, fingerprint, items_hash
ON review_plans
BEGIN
  SELECT RAISE(ABORT, 'review plan content cannot be changed after creation');
END;
