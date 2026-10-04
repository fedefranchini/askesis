# Registrazione dei dati

Dettagli richiamati da `AGENTS.md` (regola 3).

## Registrazione dei dati (CLI)

Dalla fase F1 i dati dettati in chat si registrano con la CLI, mai scrivendo nel database a mano:

```bash
bin/ak day "p 68.4 · cibo 1850 115 · pesi: panca 80x8 r2, 80x7 r1 · corsa 5.2km 31:40 fc145"
```

- `cibo` e `passi` si riferiscono al **giorno precedente**; il resto alla data di registrazione
  (`--date YYYY-MM-DD` per un altro giorno, `--dry-run` per vedere senza salvare).
- Serie dei pesi separate da **virgola + spazio**; esercizi separati da `·`.
- Correzioni: `bin/ak fix <id> '<json>'` (nuova versione); errori: `bin/ak retract <id> -r "<motivo>"`.
- Alias personali delle variabili di contesto: solo nella configurazione privata.
- Dopo ogni registrazione mostrare all'atleta la ricevuta della CLI.
- `bin/ak` è il launcher (non dipende dall'installazione editable).

## Staging (formato v0 — fallback)

Usato prima della CLI; resta valido come fallback se la CLI non è disponibile. Importazione idempotente con
`bin/ak import-staging`. I dati dettati in chat si scrivono in `data/staging/YYYY-MM.ndjson`, una riga
JSON per record, **solo in append** (`>>`). Formato envelope v0:

```json
{"id":"<uuid4>","entity_type":"body_weight","schema_version":"0.1",
 "occurred_at":"<ISO con offset>","tz":"<IANA tz>","local_date":"<YYYY-MM-DD>",
 "recorded_at":"<ora ISO con offset>","source":"manual_chat","entry_method":"manual",
 "supersedes_id":null,"missing_reason":null,"payload":{"value_kg":0.0,"fasted":true},"notes":""}
```

- `id` con `uuidgen | tr A-Z a-z`; `recorded_at` con `date -Iseconds`.
- Giorno nutrizionale: chiusura all'ora configurata (default 03:00: un pasto alle 01:30 del 5 appartiene al `local_date` del 4).
- `entity_type` MVP e payload:
  - `body_weight` {value_kg, fasted, post_void?}
  - `body_measurement` {site: "waist_navel", readings_cm: [..]}
  - `nutrition_day` {energy_kcal, protein_g, completeness: complete|partial|not_logged, logging_method: "app_estimated"}
  - `training_session` {session_id, start_at, end_at?, session_rpe?, sets: [{exercise_raw, set_type, load_kg, reps, rir?, pain?}]}
  - `running_session` {start_at, elapsed_s, moving_s?, distance_m, avg_hr?, max_hr?, run_type?, session_rpe?, stop_reason?}
  - `daily_activity` {steps}
  - `daily_context` {key, value} — variabili di contesto personalizzabili; chiavi e formato di dettatura
    definiti solo in `assistant local instructions file`
  - `sleep_session` / `resting_hr_daily` (opzionali)
  - `subjective_checkin` {fatigue_1_5?, soreness_1_5?, stress_1_5?, readiness_1_10?, illness?, pain?}
  - `athlete_attribute`, `goal`, `health_event`, `context_event` (es. `exam_period`), `test_result`
- RIR non ricordato → campo assente, **mai** inventato. Giorno nutrizionale incompleto → `partial`.
- Dopo ogni scrittura, mostrare all'atleta una ricevuta sintetica di cosa è stato registrato.

