-- askesis schema 0.3 — PLAN, DECISIONS, INTERVENTIONS, SAFETY (docs/architecture.md §4.8, §7, §8, §12, §14)
-- Everything here is append-only: state changes are new rows (events), never updates.

CREATE TABLE plan_version (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('phase', 'programme', 'nutrition_target', 'scheduled_period')),
    name             TEXT NOT NULL,          -- e.g. programme name; versions of the same plan share it
    version_no       INTEGER NOT NULL,
    valid_from       TEXT NOT NULL,          -- local date from which this version applies
    content          TEXT NOT NULL CHECK (json_valid(content)),
    content_hash     TEXT NOT NULL,
    intervention_id  TEXT,                   -- the intervention that created it (NULL only for imports)
    recorded_at      TEXT NOT NULL,
    UNIQUE (kind, name, version_no)
);

CREATE TABLE decision (
    id                        TEXT PRIMARY KEY,
    created_at                TEXT NOT NULL,
    knowledge_cutoff          TEXT NOT NULL,
    metric_run_id             TEXT,
    question                  TEXT NOT NULL,
    options                   TEXT NOT NULL CHECK (json_valid(options)),
    selected_option           TEXT,
    reasoning                 TEXT NOT NULL,
    evidence_claims           TEXT NOT NULL CHECK (json_valid(evidence_claims)),
    confidence                TEXT NOT NULL CHECK (confidence IN ('low', 'moderate', 'high')),
    status                    TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'deferred')),
    athlete_response_verbatim TEXT NOT NULL,
    responded_at              TEXT NOT NULL
);

CREATE TABLE intervention (
    id                 TEXT PRIMARY KEY,
    number             INTEGER NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    category           TEXT NOT NULL,
    origin_decision_id TEXT REFERENCES decision(id),
    prereg             TEXT NOT NULL CHECK (json_valid(prereg)),  -- frozen pre-registration
    prereg_hash        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE intervention_event (
    id               TEXT PRIMARY KEY,
    intervention_id  TEXT NOT NULL REFERENCES intervention(id),
    event            TEXT NOT NULL CHECK (event IN ('proposed', 'approved', 'activated', 'amended',
                                                     'evaluated', 'concluded', 'aborted', 'rejected')),
    at               TEXT NOT NULL,
    payload          TEXT CHECK (payload IS NULL OR json_valid(payload))
);

CREATE TABLE rule_execution (
    id                   TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,           -- e.g. double_progression@1
    local_date           TEXT NOT NULL,
    programme_version_id TEXT REFERENCES plan_version(id),
    inputs               TEXT NOT NULL CHECK (json_valid(inputs)),
    output               TEXT NOT NULL CHECK (json_valid(output)),
    recorded_at          TEXT NOT NULL
);

CREATE TABLE safety_flag (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,                    -- e.g. safety.rapid_weight_loss@1
    tier        TEXT NOT NULL CHECK (tier IN ('T0', 'T1', 'T2', 'T3')),
    local_date  TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,              -- rule + evidence window: idempotent re-evaluation
    message     TEXT NOT NULL,
    signals     TEXT NOT NULL CHECK (json_valid(signals)),
    actions     TEXT NOT NULL CHECK (json_valid(actions)),
    opened_at   TEXT NOT NULL
);

CREATE TABLE safety_resolution (
    id          TEXT PRIMARY KEY,
    flag_id     TEXT NOT NULL REFERENCES safety_flag(id),
    resolution  TEXT NOT NULL,
    at          TEXT NOT NULL
);

CREATE TABLE response_profile (
    id               TEXT PRIMARY KEY,
    intervention_id  TEXT NOT NULL REFERENCES intervention(id),
    domain           TEXT NOT NULL,
    finding          TEXT NOT NULL,                -- N-of-1, athlete-specific, never general evidence
    conclusion       TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- immutability
CREATE TRIGGER plan_version_ro_u BEFORE UPDATE ON plan_version BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER plan_version_ro_d BEFORE DELETE ON plan_version BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER decision_ro_u BEFORE UPDATE ON decision BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER decision_ro_d BEFORE DELETE ON decision BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER intervention_ro_u BEFORE UPDATE ON intervention BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER intervention_ro_d BEFORE DELETE ON intervention BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER ievent_ro_u BEFORE UPDATE ON intervention_event BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER ievent_ro_d BEFORE DELETE ON intervention_event BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER rule_exec_ro_u BEFORE UPDATE ON rule_execution BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER rule_exec_ro_d BEFORE DELETE ON rule_execution BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER sflag_ro_u BEFORE UPDATE ON safety_flag BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER sflag_ro_d BEFORE DELETE ON safety_flag BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER sres_ro_u BEFORE UPDATE ON safety_resolution BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER sres_ro_d BEFORE DELETE ON safety_resolution BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER rprof_ro_u BEFORE UPDATE ON response_profile BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER rprof_ro_d BEFORE DELETE ON response_profile BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE VIEW v_intervention_status AS
SELECT i.id, i.number, i.title, i.category,
       (SELECT e.event FROM intervention_event e WHERE e.intervention_id = i.id
        ORDER BY e.at DESC, e.rowid DESC LIMIT 1) AS status
FROM intervention i;

CREATE VIEW v_safety_open AS
SELECT f.* FROM safety_flag f
WHERE NOT EXISTS (SELECT 1 FROM safety_resolution r WHERE r.flag_id = f.id);
