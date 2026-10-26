-- App state that used to live in private files next to the database (password hash, session key, remembered
-- devices, passkeys, sync tokens, coach texts) and the access log: kept here when the database is hosted
-- (store/state.py). Mutable by design, unlike raw_record; never holds a plaintext secret, only hashes and keys.

CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,      -- "<folder>/<file name>", e.g. "data/web_devices.json", "reports/x.md"
    content     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE app_log (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT NOT NULL,
    line  TEXT NOT NULL
);
CREATE INDEX ix_app_log_key ON app_log(key, id);
