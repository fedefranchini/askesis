-- askesis schema 0.1 — RAW append-only store (docs/architecture.md §3–4, §14)

CREATE TABLE source (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,          -- manual | apple_health_export | csv | healthkit_sync | ...
    name      TEXT,
    priority  INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE ingestion_batch (
    id            TEXT PRIMARY KEY,
    adapter       TEXT NOT NULL,
    input_ref     TEXT,               -- e.g. sha256 of the input file
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    n_inserted    INTEGER NOT NULL DEFAULT 0,
    n_duplicates  INTEGER NOT NULL DEFAULT 0,
    n_rejected    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE raw_record (
    id                 TEXT PRIMARY KEY,
    entity_type        TEXT NOT NULL,
    schema_version     TEXT NOT NULL,
    occurred_at        TEXT,
    start_at           TEXT,
    end_at             TEXT,
    tz                 TEXT NOT NULL,
    local_date         TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    source_id          TEXT NOT NULL REFERENCES source(id),
    source_record_id   TEXT,
    device_id          TEXT,
    entry_method       TEXT NOT NULL,
    ingestion_batch_id TEXT NOT NULL REFERENCES ingestion_batch(id),
    supersedes_id      TEXT REFERENCES raw_record(id),
    missing_reason     TEXT,
    payload            TEXT NOT NULL CHECK (json_valid(payload)),
    payload_hash       TEXT NOT NULL,
    original_values    TEXT,
    notes              TEXT,
    UNIQUE (source_id, source_record_id)
);
CREATE INDEX ix_raw_entity_date ON raw_record(entity_type, local_date);
CREATE INDEX ix_raw_supersedes ON raw_record(supersedes_id);

-- RAW is immutable: corrections are new records (supersedes_id), removals are retractions.
CREATE TRIGGER raw_record_no_update BEFORE UPDATE ON raw_record
BEGIN SELECT RAISE(ABORT, 'raw_record is append-only'); END;
CREATE TRIGGER raw_record_no_delete BEFORE DELETE ON raw_record
BEGIN SELECT RAISE(ABORT, 'raw_record is append-only'); END;

CREATE TABLE retraction (
    id           TEXT PRIMARY KEY,
    record_id    TEXT NOT NULL REFERENCES raw_record(id),
    reason       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE TRIGGER retraction_no_update BEFORE UPDATE ON retraction
BEGIN SELECT RAISE(ABORT, 'retraction is append-only'); END;
CREATE TRIGGER retraction_no_delete BEFORE DELETE ON retraction
BEGIN SELECT RAISE(ABORT, 'retraction is append-only'); END;

-- Records that failed validation are kept for audit, never silently dropped.
CREATE TABLE rejected_record (
    id                 TEXT PRIMARY KEY,
    ingestion_batch_id TEXT NOT NULL REFERENCES ingestion_batch(id),
    raw_input          TEXT NOT NULL,
    reason             TEXT NOT NULL,
    recorded_at        TEXT NOT NULL
);

CREATE TABLE dq_issue (
    id                 TEXT PRIMARY KEY,
    record_id          TEXT REFERENCES raw_record(id),
    ingestion_batch_id TEXT REFERENCES ingestion_batch(id),
    rule               TEXT NOT NULL,
    severity           TEXT NOT NULL CHECK (severity IN ('hard', 'soft', 'info')),
    message            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'acknowledged', 'resolved', 'wont_fix')),
    created_at         TEXT NOT NULL
);

-- Current view: not superseded by a later record and not retracted.
CREATE VIEW v_current AS
SELECT r.* FROM raw_record r
WHERE NOT EXISTS (SELECT 1 FROM raw_record s WHERE s.supersedes_id = r.id)
  AND NOT EXISTS (SELECT 1 FROM retraction x WHERE x.record_id = r.id);

CREATE VIEW v_body_weight AS
SELECT id, local_date, occurred_at, source_id,
       json_extract(payload, '$.value_kg') AS value_kg,
       json_extract(payload, '$.fasted')   AS fasted
FROM v_current WHERE entity_type = 'body_weight';

CREATE VIEW v_body_measurement AS
SELECT id, local_date, occurred_at,
       json_extract(payload, '$.site') AS site,
       json_extract(payload, '$.readings_cm') AS readings_cm
FROM v_current WHERE entity_type = 'body_measurement';

CREATE VIEW v_nutrition_day AS
SELECT id, local_date, source_id,
       json_extract(payload, '$.energy_kcal')  AS energy_kcal,
       json_extract(payload, '$.protein_g')    AS protein_g,
       json_extract(payload, '$.completeness') AS completeness
FROM v_current WHERE entity_type = 'nutrition_day';

CREATE VIEW v_set_record AS
SELECT id, local_date, start_at,
       json_extract(payload, '$.session_id')  AS session_id,
       json_extract(payload, '$.sequence')    AS sequence,
       json_extract(payload, '$.exercise_raw') AS exercise_raw,
       json_extract(payload, '$.exercise_id') AS exercise_id,
       json_extract(payload, '$.set_type')    AS set_type,
       json_extract(payload, '$.load_kg')     AS load_kg,
       json_extract(payload, '$.reps')        AS reps,
       json_extract(payload, '$.rir')         AS rir,
       json_extract(payload, '$.rpe')         AS rpe
FROM v_current WHERE entity_type = 'set_record';

CREATE VIEW v_running_session AS
SELECT id, local_date, start_at, end_at,
       json_extract(payload, '$.distance_m') AS distance_m,
       json_extract(payload, '$.elapsed_s')  AS elapsed_s,
       json_extract(payload, '$.avg_hr')     AS avg_hr,
       json_extract(payload, '$.max_hr')     AS max_hr,
       json_extract(payload, '$.stop_reason') AS stop_reason
FROM v_current WHERE entity_type = 'running_session';

CREATE VIEW v_daily_context AS
SELECT id, local_date,
       json_extract(payload, '$.key')   AS key,
       json_extract(payload, '$.value') AS value
FROM v_current WHERE entity_type = 'daily_context';
