-- askesis schema 0.2 — DERIVED metrics (rebuildable cache, docs/architecture.md §5)

CREATE TABLE metric_run (
    id                 TEXT PRIMARY KEY,
    knowledge_cutoff   TEXT NOT NULL,     -- RAW recorded_at <= cutoff were visible
    computed_at        TEXT NOT NULL,     -- metadata only, never an input
    input_fingerprint  TEXT NOT NULL,     -- sha256 over the RAW records used
    params_fingerprint TEXT NOT NULL,     -- sha256 over knowledge/parameters/metrics.yaml
    engine_version     TEXT NOT NULL,
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    values_digest      TEXT NOT NULL      -- sha256 over the computed values (reproducibility check)
);

CREATE TABLE metric_value (
    run_id        TEXT NOT NULL REFERENCES metric_run(id) ON DELETE CASCADE,
    metric_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    subject       TEXT NOT NULL,          -- global | exercise:<id> | muscle:<name> | context:<key> | domain:<x>
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    value         REAL,
    unit          TEXT NOT NULL,
    lo            REAL,
    hi            REAL,
    n_obs         INTEGER NOT NULL,
    dq            REAL,
    epistemic     TEXT NOT NULL CHECK (epistemic IN ('MEASUREMENT', 'ESTIMATE', 'INFERENCE', 'FACT')),
    detail        TEXT CHECK (detail IS NULL OR json_valid(detail)),
    PRIMARY KEY (run_id, metric_id, subject, period_start, period_end)
);
CREATE INDEX ix_metric_lookup ON metric_value(metric_id, subject, period_end);

-- Latest run covering each metric/period.
CREATE VIEW v_metric_latest AS
SELECT mv.* FROM metric_value mv
JOIN metric_run r ON r.id = mv.run_id
WHERE r.computed_at = (SELECT MAX(r2.computed_at) FROM metric_run r2
                       JOIN metric_value m2 ON m2.run_id = r2.id
                       WHERE m2.metric_id = mv.metric_id AND m2.subject = mv.subject
                         AND m2.period_start = mv.period_start AND m2.period_end = mv.period_end);
