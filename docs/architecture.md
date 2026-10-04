# Askesis — Specifica architetturale

> **Versione:** v1.0 — **visione di riferimento**
> **Data:** 2026-10-03
> **Stato:** documento di visione. **Non va implementato per intero.** L'implementazione procede per fasi
> secondo il piano MVP (`docs/roadmap.md`, approvato il 2026-10-03), che **sostituisce §23** di questo documento.
> Le risposte alle domande aperte (§22) sono registrate nella roadmap.
>
> Convenzioni: **MUST/SHOULD/MAY**. `[PARAM]` = default configurabile da giustificare nella evidence KB,
> non un fatto. Negli esempi nessun numero personale: solo simboli.

---

## 1. Executive architecture

Sistema **local-first, single-athlete, a strati, append-only, bitemporale**. Il **codice deterministico
(Python) è l'unico che calcola numeri e scrive dati**; **l'assistente IA** (agente di programmazione + skill) è l'interfaccia
conversazionale e il ragionamento, e opera **solo tramite una CLI validata**.

```
 SOURCES ──► [1 INGESTION] ──► RAW (immutabile, bitemporale)
 manual · AH export · HealthKit · CSV · Strava      │
                                          [DATA QUALITY] ──► dq_issue
                                                    ▼
                                      [ANALYTICS ENGINE] ──► DERIVED (cache ricalcolabile)
                                                    ▼
                                      [STATE BUILDER] ──► ATHLETE STATE (snapshot immutabili)
                                                    ▼
                         [SAFETY GATE] ◄──────────► [DECISION ENGINE] ──► DECISIONS
                                                    ▼  (approvazione atleta)
                                      [INTERVENTION REGISTRY] ──► INTERVENTIONS
                                                    ▼
                                      PLAN (programme / nutrition-target versions)
                                                    │ (prescrizioni → aderenza osservata)
                                                    └──► torna in RAW come dati osservati

 EVIDENCE KB (separata, solo conoscenza generale) ──► consumata da Decision, Safety, Skill
 REFERENCE (catalogo esercizi, regole, zone, config) ──► consumata da tutti
```

Ai 6 layer richiesti si aggiungono **PLAN** (ciò che è *prescritto*: né osservazione né decisione;
cambia solo tramite un intervento) e **REFERENCE** (dati "di catalogo" versionati).

**Decisioni architetturali (ADR)**

| ADR | Decisione |
|---|---|
| 001 | Un solo store canonico: **SQLite** (file singolo, portabile anche su iOS) |
| 002 | RAW append-only; correzioni = **nuovi record** con `supersedes_id`; rimozioni = record di `retraction` |
| 003 | Bitemporalità: `occurred_at` (quando è accaduto) vs `recorded_at` (quando il sistema l'ha saputo) |
| 004 | **L'LLM non produce mai metriche.** Ogni numero in una risposta deve rimandare a un `metric_value` o a un record RAW |
| 005 | Le decisioni sono **proposte**; il piano cambia solo con approvazione, tranne regole già incorporate nella versione di piano attiva |
| 006 | Evidenza generale fisicamente separata dai dati dell'atleta |
| 007 | Contratto di ingestion language-agnostic (JSON Schema): l'app iOS sarà solo un altro adapter |
| 008 | La safety è **codice + istruzioni always-on**, non una skill opzionale |

---

## 2. System boundaries

**In scope:** un atleta; ingestion, storage, analisi, stato, proposte di intervento, registro interventi,
evidence KB, report.
**Out of scope:** diagnosi mediche; monitoraggio real-time (il sistema **non** è un allarme); multi-utente;
cloud sync nella v1; prescrizioni farmacologiche/integratori fuori KB.
**Attori esterni:** atleta (unico decisore finale), sorgenti dati, professionisti sanitari (solo come
destinatari di rinvio).

| Confine | Regola |
|---|---|
| Dati → LLM | Inviare al contesto **aggregati e metriche**, non stream grezzi (HR samples, GPS). Minimizzazione |
| LLM → dati | Scrittura **solo** via comandi CLI validati. Mai SQL diretto, mai editing di file in `data/` |
| Testo libero → struttura | Sintomi/infortuni detti in chat diventano record strutturati solo dopo **conferma esplicita** |
| Coaching ↔ medicina | Il sistema descrive pattern e suggerisce valutazione professionale; **non nomina diagnosi** |

---

## 3. Data flow (incluso il layer di ingestion)

```
capture → land → parse → normalize → validate → dedup/conflict → commit RAW
       → DQ soft checks → recompute DERIVED (incrementale) → build STATE
       → safety evaluate → decision triggers → report / proposte
```

### 3.1 Componente: Ingestion

- **RESPONSIBILITY:** trasformare dati di sorgente in record RAW canonici, con provenienza, idempotente.
- **INPUTS:** log manuale (grammatica CLI o JSON), file in `inbox/` (Apple Health export .zip, CSV), futuri batch NDJSON da iOS.
- **OUTPUTS:** `ingestion_batch`, record RAW, `dq_issue` bloccanti/non bloccanti, ricevuta (inseriti/duplicati/rifiutati).
- **DEPENDENCIES:** schemi canonici, modulo unità, modulo tempo, registro sorgenti, regole DQ hard.
- **MUST NOT:** interpretare trend; imputare valori mancanti; sovrascrivere record; scartare silenziosamente.

**Formato.** Contratto di ingresso = **envelope JSON** (§4.1) validato contro JSON Schema versionato.
Ogni adapter (`manual`, `apple_health_export`, `csv:<app>`, `healthkit_sync`, `strava`) mappa il formato
nativo → envelope. Il file originale **MUST** essere archiviato in `data/archive/raw/<sha256>`.

**Validazione** a 3 livelli: (1) *schema* — tipi, required, enum; (2) *hard DQ* — valori impossibili →
rifiuto + issue; (3) *soft DQ* — sospetti → accettato con flag (§12).

**Timestamp e timezone.** Istanti in **UTC** (`*_at`) + `tz` (IANA, es. `Area/Città`) + `local_date`
calcolata e salvata all'ingestion. Se la sorgente fornisce l'offset si usa quello; altrimenti la
`athlete_timezone_timeline`. **Confine del giorno:** dati giornalieri per `local_date`; **sonno** assegnato
alla *data di risveglio*; **nutrizione** con cutoff configurabile `[PARAM]` (es. 03:00). DST gestito solo
via IANA — **vietati offset fissi**.

**Unità canoniche:** massa kg · distanza m · circonferenze/altezza cm · durata s · energia kcal · HR bpm ·
HRV ms · temperatura °C. Il passo **non** è mai RAW (derivato da distanza/tempo). Valore e unità originali
conservati in `original_values`. Conversioni solo tramite modulo `units` testato.

**Provenienza:** `source_id`, `device_id`, `source_record_id`, `entry_method`
(manual/imported/synced/device_estimated), `ingestion_batch_id`, `raw_payload_ref`.

**Dati mancanti — semantica esplicita:** `NOT_RECORDED` (nessun record), `NOT_MEASURED` (dichiarato non
misurato), `NOT_APPLICABLE`, `UNKNOWN` (record presente, valore ignoto), zero reale. **Un giorno
nutrizionale non loggato ≠ 0 kcal.** Mai imputazione nel RAW.

**Duplicati:** *esatti* (stesso `source_record_id` o hash payload) → skip idempotente; *probabili* (stessa
entità, intervalli sovrapposti ≥X% `[PARAM]`, valori entro tolleranza, sorgenti diverse) → `duplicate_group`,
canonico per priorità di sorgente; *ambigui* → `dq_issue` da risolvere con l'atleta.

**Conflitti tra sorgenti:** tabella `source_priority` **per campo** (es. distanza dal Watch, HR da fascia);
consapevolezza di **lineage** (un'attività Strava nata da Apple Watch è un *duplicato*, non una conferma
indipendente; idem per app di nutrizione che scrivono anche su Salute). **Viste resolved** (`v_*_resolved`)
espongono il canonico; i perdenti restano interrogabili.

---

## 4. Canonical data model

### 4.1 Envelope comune (tutti i record RAW)

| Campo | Tipo | Req | Note |
|---|---|---|---|
| `id` | UUIDv7 | R | ordinabile nel tempo |
| `entity_type` | enum | R | |
| `schema_version` | semver | R | per-entità |
| `occurred_at` *o* `start_at`/`end_at` | timestamptz UTC | R | istante o intervallo |
| `tz` | IANA string | R | |
| `local_date` | date | R | calcolata all'ingestion |
| `recorded_at` | timestamptz | R | transaction time |
| `source_id` / `device_id` | FK | R / O | |
| `source_record_id` | string | O | ID nativo della sorgente |
| `entry_method` | enum | R | |
| `ingestion_batch_id` | FK | R | |
| `supersedes_id` | UUID | O | correzione |
| `raw_payload_ref` | string | O | sha256 + offset |
| `original_values` | json | O | valore/unità originali |
| `missing_reason` | enum | O | semantica §3 |
| `notes` | text | O | |

Tabelle di supporto: `source`, `device` (modello, firmware, tipo: watch/scale/chest_strap/phone),
`ingestion_batch`, `retraction`.

### 4.2 Athlete e contesto

| Entità | Campi principali | Tipo/Unità | Req | Source | Tempo |
|---|---|---|---|---|---|
| `athlete` | `id`, `display_name`, `birth_date`, `sex_for_formulas` | date, enum | birth_date R; resto O | manual | statico |
| `athlete_attribute` | `key` (height_cm, training_age, dietary_pattern, allergies, equipment, availability…), `value` json | per chiave | R | manual | `valid_from/valid_to` + `recorded_at` |
| `goal` | `domain`, `description`, `target_metric`, `target_value`, `target_date`, `priority_rank`, `status` | | domain, priority R | manual | `valid_from/to` |
| `health_event` | `kind` (injury/illness/symptom), `body_region`, `onset_at`, `severity` 0–10, `status`, `resolved_at`, `professional_contacted`, `professional_diagnosis_text` | | kind, onset R | manual (confermato) | intervallo |
| `context_event` | `kind` (travel, heat, work_stress, alcohol_event, schedule_disruption…), `start/end` | | R | manual/derived | intervallo |
| `supplement_intake` | `substance`, `dose`, `unit`, `taken_at`, `regimen_change` | g/mg | R | manual/HK | istante |

`health_event` e `context_event` sono essenziali per safety e **confondenti** degli interventi.

### 4.3 Body

| Entità | Campi | Unità | Req | Source tipica | Tempo |
|---|---|---|---|---|---|
| `body_weight` | `value`, `context` {fasted, post_void, clothing}, `method` | kg (0.01) | value R; context O | bilancia→AH, manual | istante |
| `body_measurement` | `site` (waist_navel, waist_narrowest, hip, chest, neck, arm_L/R, thigh_L/R…), `value`, `protocol_id`, `repeat_index` | cm | R | manual, AH (waist) | istante |
| `body_composition_estimate` | `bf_percent`, `lean_mass_kg`, `method` (BIA, DEXA, skinfold, visual) | %, kg | method R | bilancia BIA / clinica | istante, **epistemic = ESTIMATE** |
| `progress_photo` | `file_ref`, `pose`, `protocol_id` | | O | manual | istante |

### 4.4 Nutrition

| Entità | Campi | Unità | Req | Note |
|---|---|---|---|---|
| `nutrition_day` | `energy`, `protein`, `carbohydrate`, `fat`, `fiber`, `alcohol`, `sodium`, `water`, **`completeness`** (complete/partial/estimated/not_logged), `logging_method` (weighed/estimated/photo/recall) | kcal, g, mg, ml | local_date, completeness R; macro O | un solo totale canonico per giorno; se esistono entry, il totale è *derivato* |
| `nutrition_entry` | `consumed_at`, `meal_slot`, `food_name`, `quantity_g`, macro | g, kcal | O | granularità opzionale |

### 4.5 Strength

| Entità | Campi | Unità/Tipo | Req |
|---|---|---|---|
| `exercise` (REFERENCE) | `canonical_name`, `aliases[]`, `movement_pattern`, `muscle_contributions` [{muscle, role, fraction}], `equipment`, `load_type` (external/bodyweight/bw_plus/assisted), `bodyweight_fraction` `[PARAM]`, `laterality`, `is_compound`, `default_increment_kg`, `catalog_version` | | name, pattern, muscles R |
| `athlete_exercise_preference` | `exercise_id`, `status` YES/SUB/NO, `substitute_id`, `reason` | | R, `valid_from` |
| `training_session` | `start_at`, `end_at`, `session_kind`, `location`, `planned_session_id`, `session_rpe` (CR-10), `session_rpe_recorded_at` | s, 0–10 | start R |
| `set_record` | `session_id`, `exercise_id`, **`exercise_name_raw`**, `sequence`, `set_type` (warmup/working/top/backoff/drop/amrap/myo/cluster), `load_kg`, `reps`, `reps_target`, `rpe` (step 0.5) **o** `rir`, `to_failure`, `rom` (full/lengthened_partial/shortened_partial), `tempo`, `rest_before_s`, `side`, `pain` 0–10 + `pain_region`, `completed` | kg, int, s | session, exercise, set_type, load, reps, completed R; RPE/RIR O ma **fortemente raccomandato** |

Timestamp dei set: ereditato dalla sessione; `performed_at` opzionale.

### 4.6 Running e segnali cardiaci

| Entità | Campi | Unità | Req |
|---|---|---|---|
| `running_session` | `start_at`, `end_at`, `elapsed_time`, `moving_time`, `distance`, `elev_gain/loss`, `avg_hr/max_hr`, `avg_cadence`, `avg_power`, `environment` (outdoor/treadmill/track/trail), `temperature`, `run_type_declared` (easy/recovery/long/tempo/threshold/intervals/fartlek/race/test), `session_rpe`, `planned_session_id`, `shoe_id`, `route_ref`, `device_energy_kcal` (ESTIMATE), race fields | m, s, bpm, spm, W, °C | start, end, elapsed, distance R |
| `running_interval` | `session_id`, `index`, `kind` (warmup/work/recovery/cooldown/lap_auto/lap_manual), `start_offset`, `duration`, `distance`, `avg/max_hr`, `cadence`, `power`, `target` (pace/HR/RPE), `segmentation_source` | m, s, bpm | session, index, start_offset, duration R |
| `heart_rate_sample` | `ts`, `bpm`, `context` (workout/rest/sleep/unknown), `session_id` | bpm | R; tabella high-volume dedicata, mai editata |
| `resting_hr_daily` | `local_date`, `bpm`, `method` (device_algorithm/manual_morning) | bpm | R |
| `hrv_measurement` | `measured_at`, `value`, **`metric`** (sdnn/rmssd), **`context`** (sleep/morning_supine/spot), `duration` | ms | R; confronti solo entro la **stessa terna** metric+context+device |
| `vo2max_estimate` | `value`, `method` (device/lab/field_test) | ml/kg/min | R, **ESTIMATE** |

### 4.7 Sleep, recovery, subjective

| Entità | Campi | Unità | Req |
|---|---|---|---|
| `sleep_session` | `in_bed_start/end`, `sleep_onset`, `wake_at`, `asleep_duration`, `awake_duration`, `stages[]` {stage, start, end}, `assigned_date` = data di risveglio | s | inizio/fine R; stages O (**validità bassa**) |
| `subjective_checkin` | `local_date`; scale **ancorate** 1–5: fatigue, soreness (globale + regioni), stress, mood, motivation, sleep_quality; `readiness` 1–10; `illness_symptoms` (sopra/sotto il collo); `libido` O (marker REDs, sensibile); `menstrual` O (solo se applicabile e acconsentito); `free_text` | ordinale | local_date R; item O |

**"Recovery" non è un'entità RAW:** i suoi input RAW sono sonno, HRV, RHR, check-in, `health_event`;
`recovery_daily` vive in DERIVED (§5).

### 4.8 Plan (prescrizioni versionate, immutabili)

| Entità | Campi chiave | Req |
|---|---|---|
| `phase_declaration` | `phase` (fat_loss/recomposition/maintenance/muscle_gain/running_build/race_specific/taper/recovery), `goal_ids`, `valid_from/to`, `intervention_id` | R |
| `programme` | `name`, `goal_ids`, `status` | R |
| `programme_version` | `programme_id`, `version_no`, `valid_from/valid_to`, `created_by_intervention_id`, `content` (mesociclo: blocchi; microciclo template; prescrizioni per slot: `exercise_id`, serie, rep range, RIR target, regola di progressione; prescrizioni corsa: tipo, durata/distanza, target intensità; vincoli: spacing minimi, giorni), `content_hash`, `schema_version` | R |
| `planned_session` | istanze datate generate dalla versione (rigenerabili) | derivato |
| `progression_rule` (REFERENCE) | `id@version`, specifica parametrica (es. doppia progressione: range, RIR target, incremento, n esposizioni) | R |
| `rule_execution` | `date`, `rule_id@ver`, `inputs` (record id), `output_prescription` | R |
| `nutrition_target_version` | `valid_from/to`, `energy` (per tipo di giorno), `protein`, `fat_min`, `carb`, `method` (adaptive_tdee/formula_prior), `uncertainty`, `rationale`, `intervention_id` | R |

### 4.9 Derived, State, Decision, Intervention, Evidence

Specificati in §5–§9 (`metric_value`, `athlete_state_snapshot`, `decision`, `decision_option`,
`intervention`, `intervention_amendment`, `intervention_evaluation`, `evidence_source`, `evidence_claim`,
`evidence_parameter`, `dq_issue`, `safety_flag`).

---

## 5. Analytics architecture

### 5.1 Componente: Analytics engine

- **RESPONSIBILITY:** calcolo deterministico di tutte le metriche derivate, con incertezza, n osservazioni e DQ.
- **INPUTS:** viste RAW resolved fino a un `knowledge_cutoff`; REFERENCE; parametri (evidence + athlete config).
- **OUTPUTS:** `metric_value` {`metric_id`, `algorithm_version`, `subject` (global/exercise/muscle/zone), `period`, `value`, `unit`, `uncertainty` (lo/hi o sd), `n_obs`, `dq_score`, `epistemic_type`, `input_fingerprint`, `computed_at`, `knowledge_cutoff`}.
- **DEPENDENCIES:** store, units, time, data quality, metric registry.
- **MUST NOT:** leggere decisioni/interventi per *alterare* i calcoli; usare wall-clock o casualità non seminata; scrivere nel RAW; nascondere incertezza.

**Metric registry:** ogni metrica dichiarata con `id@version`, input e finestre, parametri, requisiti
minimi di dati, tipo epistemico, **test golden** su dati sintetici. Ricalcolo incrementale per "dirty
ranges" (date + propagazione finestre); `rebuild` completo **MUST** riprodurre tutto.

### 5.2 Body

| Metrica | Definizione | Min dati | Tipo |
|---|---|---|---|
| `weight_daily` | prima misura a digiuno del giorno; altrimenti la prima, con flag | 1 | MEASUREMENT |
| `weight_ma7` | media daily su 7 gg | ≥4/7 `[PARAM]` | ESTIMATE |
| `weight_ema` | EMA *gap-aware*: α_eff = 1−(1−α)^Δgiorni, α `[PARAM]` | ≥7 punti | ESTIMATE |
| `weight_rate_{14,28}` | pendenza robusta (Theil–Sen) + OLS; kg/sett e %BW/sett; IC 90% | ≥8 / ≥14 punti | ESTIMATE |
| `waist_trend` | mediana delle ripetizioni per sessione; pendenza su ≥4 sessioni in ≥3 settimane; confronto con **TEM personale** stimata dalle ripetizioni | ≥4 sessioni | ESTIMATE |
| `intake_mean` | media kcal su giorni `complete` + tasso di completezza | ≥70% giorni `[PARAM]` | MEASUREMENT (aggregato) |
| `energy_balance_est` | pendenza (kg/d) × ρ; ρ `[PARAM]` con intervallo (il classico ~7700 kcal/kg è un'approssimazione dipendente dalla composizione) | come rate | ESTIMATE, bassa confidenza |
| `adaptive_tdee` | `intake_mean − energy_balance_est` su finestra W (21–28 gg `[PARAM]`); varianza da IC pendenza × ρ + SE intake; **fusione a varianza inversa** con prior (Mifflin × fattore, varianza ampia); finestre con confondenti (inizio creatina, malattia, viaggio, grosse variazioni sodio/carbo, ciclo se applicabile) escluse o con incertezza aumentata | completezza intake ≥80%, giorni peso ≥70% `[PARAM]` | ESTIMATE |

Nota: un **bias di logging costante** viene assorbito dal TDEE adattivo — la stima è "il TDEE misurato
con il *tuo* modo di loggare", che è ciò che serve per fissare i target. Proprietà documentata, non difetto.

### 5.3 Strength

| Metrica | Definizione | Tipo |
|---|---|---|
| `hard_set` | serie `working` con RIR ≤ 4 `[PARAM]` (RIR = 10 − RPE, convenzione registrata). Senza RPE/RIR → `proximity_unknown`, conteggiata **a parte**, mai assunta hard | MEASUREMENT + convenzione |
| `volume_per_muscle_week` | Σ hard set × frazione (primario 1.0, secondario 0.5 `[PARAM]`) | ESTIMATE (proxy di stimolo) |
| `tonnage` | Σ load_eff × reps; load_eff = esterno + bw_fraction × BW | MEASUREMENT derivata |
| `reps`, `load`, `mean_rpe` | per esercizio/sessione | MEASUREMENT |
| `e1rm_set` | Epley con reps_eff = reps + RIR, valida se reps_eff ≤ 12 `[PARAM]`; senza RIR → "lower bound" con RIR=0, flag | ESTIMATE |
| `e1rm_session` | max sui set validi | ESTIMATE |
| `performance_trend` | Theil–Sen su `e1rm_session` nelle ultime N esposizioni (≥4) o 6 settimane; eventi PR di ripetizioni a carico | ESTIMATE |
| `plateau` | IC pendenza ∋ 0 **e** nessun PR in ≥k esposizioni `[PARAM]` | INFERENCE |
| `fatigue_indicators` | (a) **RPE drift** a carico/reps uguali ≥ +1 su 2 esposizioni; (b) e1RM < best mobile 4 sett −X% `[PARAM]`; (c) drop-off intra-sessione; (d) reps target mancate | INFERENCE |
| `strength_adherence` | serie completate / pianificate | MEASUREMENT |

### 5.4 Running

| Metrica | Definizione | Tipo |
|---|---|---|
| `distance`, `moving_time`, `pace` (s/km), `speed` | derivate dal RAW (GAP in fase successiva) | MEASUREMENT |
| `zone_model` (REFERENCE, versionato) | metodo (%HRmax / HRR / LTHR / soglie lab); ancore con data e **origine** (test > formula età, flaggata bassa qualità) | FACT (config) |
| `time_in_zone` | dai campioni HR ricampionati, senza interpolare gap > X s `[PARAM]`; senza campioni → classificazione per avg HR o tipo sessione, DQ ridotto | MEASUREMENT/ESTIMATE |
| `srpe_load` | RPE (CR-10) × minuti (Foster) | MEASUREMENT derivata |
| `trimp` | Banister, se HR disponibile | ESTIMATE |
| `weekly_volume` | km, tempo, n corse, quota del lungo, load (settimana ISO lun–dom `[PARAM]`) | MEASUREMENT |
| `intensity_distribution` | 3 zone Z1/Z2/Z3, per time-in-zone **e** per session-goal; confronto con piano | ESTIMATE |
| `progression` | variazione % settimanale, media mobile 4 sett, monotony & strain (Foster); ACWR **solo descrittivo**, non predittore di infortunio (validità contestata) | ESTIMATE |
| `aerobic_efficiency` | velocità/HR su corse easy stazionarie, filtrate per temperatura/terreno | ESTIMATE, bassa confidenza |
| `performance` | risultati gara/test; VO2max device (stima esterna) | MEASUREMENT/ESTIMATE |

### 5.5 Recovery e sleep

| Metrica | Definizione | Tipo |
|---|---|---|
| `sleep_duration` | asleep giornaliero, media 7 gg, efficienza | MEASUREMENT (wearable: validità moderata) |
| `sleep_consistency` | SD del punto medio del sonno e dell'onset su 7/14 gg (SRI richiede dati a epoche: fase successiva) | ESTIMATE |
| `rhr_status` | media 7 gg e z-score vs baseline 28–60 gg `[PARAM]` | ESTIMATE |
| `hrv_status` | ln(valore), media mobile 7 gg vs baseline ± SWC (0.5 × SD) `[PARAM]`, **per serie omogenea**; SDNN Apple a timing irregolare = validità ridotta, flag | ESTIMATE |
| `subjective_series` | ogni item come serie a sé, deviazione dalla baseline personale | MEASUREMENT |
| `readiness_composite` | **opzionale**; pesi documentati; epistemic = HYPOTHESIS finché non validato sull'atleta | HYPOTHESIS |

### 5.6 Concurrent training

| Metrica | Definizione | Tipo |
|---|---|---|
| `session_timeline` | tutte le sessioni (pesi/corsa) su un unico asse | FACT |
| `spacing` | ore tra fine sessione "lower-body hard" (≥k hard set lower `[PARAM]`) e inizio corsa "hard" (tipo intensità o load > p75), e viceversa; flag sotto soglia `[PARAM collegato a evidenza]` | MEASUREMENT + regola |
| `fatigue_budget` | load per **canale**: sistemico (sRPE totale), locale lower (hard set lower + run load × peso `[PARAM]`), locale upper; acuto 7 gg vs cronico 28 gg; budget = cronico × k | **HYPOTHESIS** (costrutto euristico, non validato) |
| `interference_signal` | (a) residuo e1RM lower vs trend in funzione del run load delle 48 h precedenti; (b) qualità della corsa dopo sessione lower pesante; riportato solo con ≥N coppie `[PARAM]`, con effetto e IC | INFERENCE esplorativa |
| `recovery_constraint_violations` | violazioni dei vincoli scritti nella versione di piano | FACT |

---

## 6. Athlete State Model

### 6.1 Componente: State builder

- **RESPONSIBILITY:** produrre uno snapshot sintetico e immutabile dello stato a una data, con tipo epistemico, confidenza e DQ per campo.
- **INPUTS:** `metric_value` (≤ `knowledge_cutoff`), PLAN attivo (fase dichiarata, target), `health_event`, `context_event`, `safety_flag`.
- **OUTPUTS:** `athlete_state_snapshot` {`id`, `as_of_date`, `knowledge_cutoff`, `builder_version`, `metric_fingerprint`, `fields`, `overall_dq`}.
- **DEPENDENCIES:** analytics, plan, DQ.
- **MUST NOT:** calcolare metriche nuove (solo leggerle); proporre interventi; promuovere ipotesi a fatti.

### 6.2 Tipi epistemici

| Tipo | Definizione | Esempio |
|---|---|---|
| **FACT** | dichiarato o vero per definizione/registrazione | "programma v3 attivo", "infortunio segnalato" |
| **MEASUREMENT** | osservazione strumentale o registrata | peso letto, set loggato |
| **ESTIMATE** | output di un modello con incertezza | TDEE adattivo, e1RM, VO2max Watch |
| **INFERENCE** | conclusione basata su regole e stime | "plateau su panca", "deficit coerente con la fase" |
| **HYPOTHESIS** | spiegazione causale non testata | "plateau dovuto al volume di corsa" |

Regole: un'INFERENCE MUST citare le stime da cui deriva; un'HYPOTHESIS MUST essere collegata a un
intervento o marcata `untested`; la conclusione di un intervento può marcarla `supported_n_of_1` /
`not_supported_n_of_1` — **mai** evidenza generale.

### 6.3 Campi dello stato

Ogni campo: `{value, epistemic_type, confidence, dq_grade, derived_from[], valid_window}`.

| Campo | Valori | Tipo | Fonte |
|---|---|---|---|
| `current_phase` | enum | FACT | `phase_declaration` |
| `phase_consistency` | consistent / drifting / inconsistent | INFERENCE | weight rate vs target fase |
| `body_composition_trend` | {rate peso, trend vita, direzione: losing_fat_likely / recomp_possible / gaining / unclear} | ESTIMATE/INFERENCE | §5.2 |
| `energy_balance` | stima ± incertezza | ESTIMATE | adaptive_tdee |
| `energy_availability_risk` | low / moderate / high / unknown | INFERENCE | proxy (§11); EA vera richiede FFM ed EEE: dichiararlo |
| `strength_status` (per lift) | progressing / plateau / regressing / insufficient_data | INFERENCE | §5.3 |
| `hypertrophy_status` | volume/muscolo vs target, trend proxy (e1RM in range moderati, circonferenze) | INFERENCE, **confidenza bassa per costruzione** (ipertrofia non misurabile direttamente senza imaging) | §5.3, §5.2 |
| `running_status` | volume, distribuzione intensità vs piano, progressione, efficienza | ESTIMATE/INFERENCE | §5.4 |
| `recovery_status` | normal / strained / degraded / unknown | INFERENCE | §5.5 |
| `fatigue_status` | per canale (sistemico/lower/upper) | INFERENCE/HYPOTHESIS | §5.3, §5.6 |
| `adherence` | training (sessioni, serie), running, completezza log nutrizione, aderenza target | MEASUREMENT | plan vs raw |
| `active_interventions` | lista + stato | FACT | registry |
| `active_safety_flags` | lista + tier | FACT | safety |
| `confidence` | complessiva + per campo | — | §7.5 |
| `data_quality` | grade per dominio (A–D) | — | §12 |

---

## 7. Decision Engine

### 7.1 Componente

- **RESPONSIBILITY:** trasformare stato e problemi rilevati in **proposte tracciabili**, valutate e passate dal safety gate.
- **INPUTS:** state snapshot, catalogo problemi, rule library, evidence KB, interventi attivi, safety flag, richiesta atleta.
- **OUTPUTS:** record `decision` (con opzioni) in stato `proposed`; dopo approvazione → creazione intervento.
- **DEPENDENCIES:** state, safety, evidence, intervention registry, skill di dominio (arricchimento opzioni).
- **MUST NOT:** modificare il PLAN senza approvazione (salvo L1); proporre opzioni bloccate dalla safety; usare numeri non tracciabili; decidere con DQ sotto la soglia del tipo di decisione.

### 7.2 Pipeline

| Stadio | RESP | IN | OUT | Chi |
|---|---|---|---|---|
| **OBSERVE** | fissare `knowledge_cutoff` e snapshot | trigger | `state_snapshot_id` | codice |
| **ANALYZE** | selezionare metriche rilevanti + DQ | snapshot | analysis bundle | codice |
| **IDENTIFY PROBLEM** | applicare le regole del catalogo problemi | bundle | `problem_instance[]` | codice |
| **GENERATE OPTIONS** | opzioni da rule library + arricchimento skill; **sempre** incluse "nessun cambio / raccogliere dati" e "correggere la misura" | problemi | `option[]` | codice + LLM |
| **EVALUATE OPTIONS** | scoring (§7.4) + filtro safety | opzioni | opzioni valutate | codice (score), LLM (motivazione) |
| **PROPOSE** | comporre la decisione; il validatore verifica riferimenti e numeri | opzione scelta | `decision` (proposed) | codice + LLM |
| **MONITOR** | checkpoint, early-stop | intervento attivo | `intervention_evaluation` (interim) | codice |
| **REASSESS** | alla data di valutazione: esito vs atteso | finestra | conclusione | codice + LLM |

**Trigger:** review settimanale (schedulata), evento (nuovi dati che attivano una regola), safety flag,
richiesta dell'atleta.

### 7.3 Livelli di automazione

| Livello | Cosa | Approvazione |
|---|---|---|
| **L0** | descrittivo (report) | — |
| **L1** | esecuzione di una regola **già incorporata** nella versione di piano attiva (es. doppia progressione, deload pianificato) | no — registrata come `rule_execution` |
| **L2** | qualsiasi cambio a target calorici/macro, struttura, volume, fase, esercizi, volume di corsa | **sempre** |
| **L3** | bloccato/escalato dalla safety | — |

### 7.4 Valutazione delle opzioni

Criteri: effetto atteso (direzione e ordine di grandezza), grado dell'evidenza, tempo-al-segnale,
costo/carico per l'atleta, rischio, reversibilità, **impatto sull'attribuibilità** (principio "**una
variabile alla volta per dominio di esito**", per mantenere l'intervento valutabile).

### 7.5 Record `decision`

| Campo | Contenuto |
|---|---|
| `id`, `created_at`, `trigger` | |
| `knowledge_cutoff`, `state_snapshot_id` | "cosa sapevamo" |
| `problem_instances` | id catalogo + metriche scatenanti |
| `data_used` | id `metric_value` + query RAW con fingerprint |
| `evidence_used` | `claim_id@version` + grado |
| `options` | per ciascuna: descrizione, punteggi, rischi, motivo di scarto |
| `selected_option`, `reasoning` | testo con riferimenti verificati dal validatore |
| `confidence` | low/moderate/high = **min**(DQ grade, evidence grade, forza del segnale vs rumore), con motivazione |
| `expected_outcome` | metrica, direzione, intervallo plausibile, entro quando |
| `reassessment_window` | data/i di valutazione + criteri di early-stop |
| `safety_check` | esito + regole id |
| `status` | proposed / accepted / rejected / deferred / expired / superseded + `athlete_response_at` |
| `intervention_id` | se accettata |
| `provenance` | versioni regole, skill, modello LLM, prompt |

**Il rifiuto si registra quanto l'accettazione.**

### 7.6 Catalogo problemi (esempi, versionato)

`P-BODY-01` rate fuori target di fase · `P-BODY-02` perdita troppo rapida (→ safety) · `P-STR-01` plateau
· `P-STR-02` accumulo fatica · `P-HYP-01` volume/muscolo sotto/sopra il range del piano · `P-RUN-01` ramp
volume troppo rapido · `P-RUN-02` drift della distribuzione d'intensità · `P-REC-01` debito/irregolarità di
sonno · `P-REC-02` RHR/HRV fuori baseline persistente · `P-CONC-01` violazione di spacing · `P-ADH-01`
calo di aderenza · `P-DQ-01` dati insufficienti per una decisione richiesta. Ognuno: regola di rilevamento
deterministica, DQ minimo, severità, skill proprietaria.

---

## 8. Intervention Registry

### 8.1 Componente

- **RESPONSIBILITY:** registro longitudinale **pre-registrato** di ogni cambiamento al piano e della sua valutazione.
- **INPUTS:** decisioni accettate, cambi avviati dall'atleta, eventi esterni forzanti (malattia), metriche.
- **OUTPUTS:** `intervention`, `intervention_amendment`, `intervention_evaluation`, nuove versioni di PLAN, voci dell'**Athlete Response Profile**.
- **DEPENDENCIES:** analytics, state, plan, decision.
- **MUST NOT:** scegliere interventi; modificare campi pre-registrati dopo l'attivazione; dichiarare causalità che il disegno non supporta.

### 8.2 Schema

| Campo | Note |
|---|---|
| `id`, `title`, `category` | nutrition_energy, nutrition_macro, training_volume/intensity/frequency, exercise_selection, running_volume/intensity, scheduling, recovery_sleep, deload, supplement, measurement_protocol, other |
| `origin` | `decision_id` / athlete_initiated / external_forced |
| `hypothesis`, `mechanism` | collegati a claim di evidenza |
| `baseline` | `state_snapshot_id` + **valori congelati** delle metriche chiave + loro DQ |
| `change` | diff strutturato: PLAN version from→to, target from→to |
| `reason` | |
| `expected_outcome` | metrica primaria, direzione, **effetto minimo rilevante**, intervallo atteso; metriche secondarie |
| `success_criteria` / `stop_criteria` | stop include soglie di safety |
| `min_adherence` | sotto soglia → esito `inconclusive_low_adherence` |
| `start_date`, `interim_checks[]`, `evaluation_date` | |
| `actual_outcome` | calcolato: valori, delta vs baseline e vs traiettoria-baseline proiettata |
| `adherence_actual` | |
| `confounders` | **automatici** (interventi sovrapposti, `health_event`, `context_event`, buchi di dati, cambio device, cambio integratori) + manuali |
| `conclusion` | effective / not_effective / inconclusive / confounded / stopped_for_safety / aborted |
| `conclusion_confidence`, `learnings` | |
| `status` | draft → proposed → accepted → **active** (freeze) → evaluating → concluded |

### 8.3 Regole

- **Pre-registrazione:** ipotesi, esito atteso, criteri e finestra sono immutabili da `active`; modifiche = `amendment` datati con motivo.
- **Concorrenza:** max 1 intervento attivo per dominio di esito primario `[PARAM]`; altrimenti la conclusione è marcata "attribuzione ambigua".
- **Valutazione:** traiettoria osservata vs traiettoria estrapolata dalla baseline, con soglie di rumore (TEM, SWC, IC); linguaggio "compatibile con", mai "dimostra" — il disegno pre/post N-of-1 è debole e va dichiarato.
- **Athlete Response Profile:** conclusioni N-of-1 (es. "risposta storica ad aumenti di volume"), marcate *athlete-specific*, **mai** fuse nella evidence KB generale.

Query garantite: "Quando abbiamo cambiato X? Perché? Che dati avevamo? Cosa è successo? Cosa c'era in
parallelo?" → `intervention` → `decision` → `state_snapshot` → `metric_value` → RAW.

---

## 9. Evidence architecture

### 9.1 Componente

- **RESPONSIBILITY:** knowledge base generale, claim-centrica, verificata e graduata, che alimenta parametri e motivazioni.
- **INPUTS:** paper, position stand, review (via skill `evidence-review`), identificativi verificabili.
- **OUTPUTS:** `evidence_source`, `evidence_claim@version`, `evidence_parameter`.
- **DEPENDENCIES:** nessuna verso i dati dell'atleta (**isolamento rigido**).
- **MUST NOT:** contenere dati dell'atleta; marcare `verified` senza identificativo risolvibile; trattare una position stand come livello di evidenza superiore di per sé.

### 9.2 Schema

- **`evidence_source`**: id, type (meta-analysis / systematic review / RCT / cohort / position stand / narrative review / textbook), autori, anno, titolo, rivista, **DOI/PMID/URL**, data di pubblicazione, retraction flag, `verified_at` (DOI risolto via Crossref/PubMed).
- **`evidence_claim`** (versionato): `claim` (affermazione atomica), `domain_tags`, `conditions/scope`, `population` (training status, sesso, età, N), `outcome`, `effect` (direzione, size se disponibile), `evidence_type`, **`certainty`** (high/moderate/low/very_low, ispirato a GRADE), `limitations`, `relevance` (al dominio, non all'atleta), `links` [{source_id, relation: supports/contradicts/qualifies}], `status` (unverified/verified/contested/superseded/retracted), `last_reviewed`, `review_due` `[PARAM, es. 18 mesi]`, `reviewer` (umano/assistente IA + data).
- **`evidence_parameter`**: id (es. `protein_g_per_kg_range`), default value/range, unità, `derived_from` [claim_id@ver], razionale.
- **Override dell'atleta**: in `config/athlete_overrides`, con motivo e `intervention_id` — **separazione garantita** tra parametro generale e override personale.

Formato: YAML in `knowledge/evidence/` (sotto git, revisionabile), indicizzato nel DB all'avvio.

---

## 10. Skill architecture

**Principi trasversali:** le skill **non calcolano** e **non scrivono** direttamente — chiamano la CLI
`askesis`. Descrizioni **strette**; solo l'orchestratore ha un trigger ampio dentro il progetto. Ogni skill ha
una sezione esplicita "Non fare".

**Valutazione dell'elenco proposto:**
- **`hypertrophy` + `strength` → `resistance-training`** (una skill, due reference). Progressione, selezione esercizi, conteggio del volume e deload sono condivisi: due skill duplicherebbero logica e si contraddirebbero.
- **`recovery` + `sleep` → `recovery-sleep`**: il sonno è input della recovery; stesse soglie e segnali.
- **Safety non è una skill**: modulo di codice + regole always-on nel file di istruzioni dell'assistente (`AGENTS.md`) (una skill può non attivarsi). Al massimo una reference di linguaggio per i rinvii, letta dall'orchestratore.

| Skill | RESPONSIBILITY | INPUTS | OUTPUTS | ACTIVATION | DEPENDENCIES | MUST NOT |
|---|---|---|---|---|---|---|
| **coach-orchestrator** | entry point coaching; workflow review settimanale; routing alle skill di dominio; contratto di risposta (etichette epistemiche, riferimenti ai dati, citazioni) | richiesta, snapshot, safety flag (via CLI) | risposte; bozze di decision via CLI | domande di coaching nel progetto ("come sto andando", "review", "cosa cambio") | tutte; safety (codice) | contenere metodologia di dominio; calcolare; modificare il piano senza approvazione |
| **resistance-training** | metodologia ipertrofia + forza: volume, prossimità al cedimento, frequenza, selezione esercizi, modelli di progressione, deload, diagnosi plateau | snapshot (strength/hypertrophy), versione di piano, preferenze esercizi, evidenza | opzioni di dominio, bozze di programme_version | richiesta dall'orchestratore o domanda specifica sui pesi | analytics, evidence | decidere su corsa o nutrizione; scavalcare i vincoli di concurrent-training; stimare e1RM nel testo |
| **running** | zone, distribuzione dell'intensità, progressione del volume, tipi di sessione, protocolli di test, preparazione gara | snapshot running, zone model, evidenza | opzioni, bozze di blocchi corsa | domanda specifica sulla corsa | analytics, evidence | decisioni sui pesi; target calorici; diagnosi di infortuni |
| **concurrent-training** | **unica proprietaria** dell'arbitrato cross-modale: priorità tra obiettivi, scheduling, spacing, allocazione del budget di fatica | opzioni da resistance-training e running, stato, goal | struttura settimanale vincolata, vincoli per la programme_version | quando un'opzione tocca più modalità o lo scheduling | le due precedenti, analytics | inventare prescrizioni specifiche di modalità (delega) |
| **sports-nutrition** | strategia di fase (cut/recomp/mantenimento/bulk), rate target, energia e macro, fueling per la corsa, integratori in KB | adaptive TDEE, stato corpo, fase, safety | opzioni nutrizionali, bozze di nutrition_target_version | domande su calorie, macro, fase, peso | analytics, evidence, safety | calcolare il TDEE; andare sotto i floor di safety; diagnosticare ED o condizioni mediche; moralizzare |
| **recovery-sleep** | interpretazione HRV/RHR/sonno/soggettivi; readiness; igiene del sonno; segnali per deload reattivo | stato recovery | segnali, opzioni di recovery | domande su sonno, stanchezza, readiness; problemi P-REC | analytics, evidence | diagnosticare disturbi del sonno (segni di apnea → rinvio); scavalcare la safety |
| **athlete-analytics** | lettura e spiegazione di metriche, trend, incertezza, DQ; sintesi analitiche | metric_value, DQ | spiegazioni, grafici (via modulo report) | "mostrami", "trend", "perché questo numero" | analytics engine | raccomandare interventi; calcolare fuori dall'engine |
| **evidence-review** | ricerca, appraisal, grading, aggiornamento claim, verifica DOI | paper, identificativi | claim/parameter via CLI | "cosa dice la letteratura", aggiornamenti KB | evidence store | usare dati dell'atleta; dare raccomandazioni di coaching; marcare verified senza ID |
| **health-data** | ingestion: grammatica del log manuale, import (AH export, CSV), mapping, risoluzione DQ issue | file, testo di log | ingestion batch, ricevute | "logga", "importa", "ho un export" | ingestion, DQ | interpretare trend; fare coaching; modificare il RAW fuori dalla supersession |
| **intervention-management** | lifecycle: creazione da decisione accettata, controllo completezza pre-registrazione, valutazioni, conclusioni, query storiche | decisioni, metriche | interventi, valutazioni | "perché abbiamo cambiato", "valuta l'intervento", scadenze | registry, analytics | scegliere interventi; alterare campi pre-registrati |

**Ownership dei temi trasversali (anti-sovrapposizione)**

| Tema | Rileva | Propone | Arbitra/valida | Registra |
|---|---|---|---|---|
| Deload | analytics (fatica) | resistance-training / running | concurrent-training + safety | decision → intervention |
| Calorie | analytics (TDEE) | sports-nutrition | safety (floor) | idem |
| Spacing | analytics | concurrent-training | safety | idem |
| Sonno | analytics | recovery-sleep | orchestrator | idem |
| Citazioni | — | evidence-review | validatore | evidence store |

---

## 11. personal-trainer integration

Stato: **reference e knowledge source, mai source of truth, non attiva durante lo sviluppo.**

| Parte | Azione | Nota |
|---|---|---|
| Policy evidenze (grading, "mai inventare citazioni", rifiuto pseudoscienza) | **Riutilizzare** | nelle regole dell'orchestratore |
| Principi di `evidence.md` (volume, prossimità al cedimento, proteine, bilancio energetico, timing secondario) | **Adattare** | importati come claim `status=unverified` → verifica via evidence-review |
| Doppia progressione | **Adattare** | → `progression_rule` parametrica (incrementi relativi al carico/esercizio, non fissi a +5 kg) |
| Checklist stalli di peso | **Adattare** | → albero diagnostico di `P-BODY-01`, con i controlli DQ come primo ramo |
| Exercise library YES/SUB/NO | **Adattare** | → `athlete_exercise_preference` + catalogo `exercise` con contributi muscolari |
| Campi del profilo atleta | **Adattare** | → checklist di onboarding per `athlete_attribute`/`goal`/`context` |
| Confine medico | **Riutilizzare + estendere** | → safety layer |
| Prompt stack | **Adattare** | → esempi di intent per le skill |
| Cartella `coach-data/` markdown | **Ignorare** | seconda fonte di verità |
| Mifflin × fattore come TDEE | **Ignorare come target**; riusare solo come *prior* | |
| Deficit fisso 300–500 kcal | **Ignorare** | → rate relativo al peso, adattivo |
| Blocco default 12 settimane (forza) | **Ignorare** come default | |
| Persona "numeri non range, niente disclaimer" | **Ignorare** | in conflitto con ADR-004 e l'incertezza esplicita |
| Unità miste (lb nei macro) | **Ignorare** | solo SI |

---

## 12. Safety architecture

### 12.1 Componente

- **RESPONSIBILITY:** riconoscere segnali di rischio, assegnare un tier, bloccare categorie di intervento, produrre rinvii; audit completo.
- **INPUTS:** stato, metriche, `health_event`, check-in, testo libero dell'atleta (riconosciuto dall'LLM → **confermato** come record strutturato).
- **OUTPUTS:** `safety_flag` {rule@ver, segnali, tier, azioni, `opened_at`, `resolved_at`, `resolution`}; vincoli per il decision engine.
- **DEPENDENCIES:** analytics, state, regole versionate (`knowledge/rules/safety/`).
- **MUST NOT:** diagnosticare; nominare patologie come conclusione; essere disattivabile da una skill; dipendere solo dall'LLM.

### 12.2 Tier

| Tier | Significato | Azione |
|---|---|---|
| T0 | info | annotazione nel report |
| T1 | caution | blocca interventi che aumentano lo stress (deficit, volume, intensità) |
| T2 | pausa + raccomandazione di valutazione professionale | sospende gli interventi attivi interessati |
| T3 | **urgente** | messaggio di stop + indicazione di rivolgersi a un medico / servizi di emergenza; il sistema dichiara di non essere un monitor real-time |

### 12.3 Regole (soglie tutte `[PARAM]`, collegate a evidenza)

| Rischio | Segnali | Tier tipico |
|---|---|---|
| Infortunio | `pain` ≥ X nel set, dolore persistente/peggiorativo, `health_event` injury | T1–T2 |
| Malattia | sintomi sotto il collo, febbre, RHR elevata + sintomi | T1 (sopra il collo) / T2 |
| Fatica eccessiva | più indicatori di fatica + recovery degradata per > N giorni | T1 |
| Calo persistente di performance | e1RM/efficienza aerobica in calo su più settimane senza spiegazione di piano | T1 → T2 |
| Perdita di peso troppo rapida | rate > X %BW/sett per ≥ N settimane | T1 / T2 |
| Rischio bassa disponibilità energetica | intake basso vs spesa stimata, calo performance, fatica, malattie/infortuni frequenti, calo libido, RHR anomala, **disfunzione mestruale se applicabile** (riferimento: consenso IOC 2023 su REDs / REDs CAT2 — **da verificare in KB**) | T1/T2 |
| Segnali REDs | combinazione dei precedenti + infortuni da stress osseo | T2 |
| Segnali di disturbo alimentare | restrizione rigida/crescente, esercizio compensatorio, angoscia per sessioni/log mancati, linguaggio di abbuffata/compensazione, intake molto basso | T2 + **disattivare i target calorici restrittivi**, offrire riduzione del tracking, linguaggio non giudicante, rinvio |
| Sintomi cardiovascolari | dolore/oppressione toracica, svenimento o quasi durante lo sforzo, palpitazioni con capogiro, dispnea sproporzionata, notifiche di ritmo irregolare | **T3** |

**Rischio indotto dal sistema:** il tracking ossessivo è esso stesso un rischio → il sistema MUST
consentire modalità a basso logging e non "premiare" mai restrizioni estreme.
**Linguaggio:** il coaching dice "questo pattern merita una valutazione"; **non** dice "hai X".

---

## 13. Data quality architecture

### 13.1 Componente

- **RESPONSIBILITY:** rilevare e assegnare un punteggio ai problemi di qualità, per record e per analisi.
- **INPUTS:** RAW, metadati di sorgente/device.
- **OUTPUTS:** `dq_issue` {tipo, gravità, record, stato: open/acknowledged/resolved/wont_fix}, flag sul record, `dq_score` per metrica/analisi.
- **DEPENDENCIES:** store, units, registro sorgenti.
- **MUST NOT:** correggere valori silenziosamente; cancellare record.

### 13.2 Controlli

| Tipo | Esempio di regola |
|---|---|
| Missing | completezza attesa vs osservata per cadenza (peso giornaliero, nutrizione giornaliera, check-in) |
| Impossibili (hard) | peso ∉ [30, 250] kg, HR ∉ [25, 230], RPE ∉ [1, 10], reps > 100, passo più veloce di un limite umano |
| Outlier (soft) | filtro di Hampel / z-score robusto (MAD) sul peso; salto giornaliero > X kg |
| Unità incoerenti | euristiche di scala (peso ≈ 2.2× la serie → probabili lb) |
| Duplicati | §3 |
| Sorgenti in conflitto | disaccordo oltre tolleranza tra sorgenti per lo stesso intervallo |
| Trend sospetti | log nutrizionali identici ogni giorno; log solo nei "giorni buoni" (completezza correlata all'intake); **gradino coincidente con cambio device** (changepoint allineato a `device_id`) |

### 13.3 Score

Componenti 0–1: **completeness**, **validity** (1 − quota flaggata), **consistency** (stabilità
sorgente/device, nessun conflitto di unità), **timeliness**, **precision** (tier sorgente:
strumentale > manuale > stima device). **Grade = determinato dal componente minimo** (A se tutti ≥ 0.9;
B ≥ 0.75; C ≥ 0.5; altrimenti D) per non mascherare debolezze.
**Gating:** ogni tipo di decisione dichiara un grade minimo (es. cambio calorie richiede ≥ B su peso e
intake per ≥ 14 giorni `[PARAM]`); sotto soglia l'unica opzione ammessa è `P-DQ-01` (migliorare la raccolta).

---

## 14. Versioning strategy

| Oggetto | Meccanismo |
|---|---|
| **Schemi** | semver per entità; `schema_version` su ogni record; migrazioni SQL numerate forward-only; *upcaster* per leggere versioni vecchie; JSON Schema pubblicati in `schemas/vN/` |
| **RAW** | bitemporale (`occurred_at`/`recorded_at`); correzioni via `supersedes_id`; `retraction`; query "as-of" su entrambi i tempi |
| **Metriche** | `metric_id@algorithm_version` + `input_fingerprint`; cambiare algoritmo crea nuovi valori, i vecchi restano |
| **State** | snapshot immutabili con `knowledge_cutoff` + `builder_version` |
| **Programmi/target** | `*_version` immutabili con `valid_from/valid_to` + `recorded_at` + `content_hash` + `intervention_id` |
| **Decisioni** | append-only; cambi di stato come eventi |
| **Interventi** | campi pre-registrati congelati; `amendment` versionati |
| **Evidenza** | `claim@version`; storia in git + `last_reviewed` |
| **Regole** (progression, problem, safety, DQ) | `rule_id@version`, referenziate da esecuzioni e decisioni |
| **Codice, skill, KB** | git; ogni decisione registra commit hash e versioni delle skill |

**Ricostruzioni:**
- *"Quale programma era attivo il 12 marzo?"* → `programme_version WHERE valid_from ≤ data < valid_to`; variante bitemporale "cosa *credevamo* fosse attivo" aggiunge `recorded_at ≤ t`.
- *"Cosa sapevamo quando abbiamo deciso?"* → `decision.knowledge_cutoff` + `state_snapshot_id`; replay del RAW con `recorded_at ≤ cutoff` e versioni di algoritmo bloccate, **verificato** contro l'`input_fingerprint` salvato.

---

## 15. Future Apple Health / HealthKit architecture

**Principio:** HealthKit è *un adapter come gli altri*; il contratto è l'envelope JSON Schema → nessun layer
a valle cambia.

| Fase | Meccanismo | Note |
|---|---|---|
| **A — Export** | iPhone → Salute → Esporta → `export.zip` (`export.xml`, `workout-routes/*.gpx`, …) → `inbox/` → parser **streaming** (XML può pesare GB) | nessuna app necessaria; refresh manuale periodico |
| **B — Companion iOS** | app Swift: HealthKit read via `HKAnchoredObjectQuery` (sync incrementale con *anchor*, gestisce **campioni cancellati**), background delivery; scrive batch NDJSON conformi all'envelope in una cartella di sync (iCloud Drive o endpoint locale) → ingestion sul Mac | l'engine resta sul Mac; l'app fa solo capture + display |
| **C — Watch** | eventuale app di logging dei set in palestra | opzionale |

**Mapping (esempi):** `BodyMass`→`body_weight`; `WaistCircumference`→`body_measurement`;
`BodyFatPercentage`→`body_composition_estimate`; `HeartRate`→`heart_rate_sample`;
`RestingHeartRate`→`resting_hr_daily`; `HeartRateVariabilitySDNN`→`hrv_measurement(metric=sdnn)`;
`VO2Max`→`vo2max_estimate`; `SleepAnalysis` (inBed, asleepCore/Deep/REM/Unspecified, awake) →
`sleep_session.stages`; `HKWorkout` running + eventi lap/segment → `running_session`/`running_interval`;
metriche di corsa (`RunningPower`, `RunningSpeed`, stride, ground contact) → campi opzionali;
`Dietary*` → `nutrition_day`; `ActiveEnergyBurned`/`BasalEnergyBurned` → ESTIMATE.

**Limiti noti:** HealthKit **non contiene serie/ripetizioni** → i dati di palestra restano manuali o da
export CSV di un'app di lifting. Un'app di nutrizione che scrive anche su Salute **non va importata due
volte** (dedup per `sourceName` + regola di lineage). L'export XML potrebbe **non includere gli UUID** dei
campioni (da verificare): in tal caso la dedup tra export e sync live usa la chiave naturale (tipo, start,
end, valore, sorgente). Calorie del device e fasi del sonno: validità limitata, marcate come tali.
**Privacy:** dati locali, solo aggregati verso il contesto LLM, backup cifrati.

---

## 16. Recommended technology stack

| Area | Scelta | Motivo |
|---|---|---|
| Linguaggio core | **Python 3.12+**, gestito con `uv` | ecosistema analitico, test |
| Store | **SQLite** (WAL) | file singolo, transazionale, leggibile da Swift (GRDB) |
| Analisi su grandi volumi | DuckDB (opzionale, in seguito) | legge SQLite/Parquet senza ETL |
| Modelli/contratti | **Pydantic v2** → export JSON Schema | una sola definizione dei contratti |
| Trasformazioni | Polars, NumPy, SciPy | determinismo, performance |
| Unità | `pint` dietro il modulo `units` | conversioni testate |
| Parsing XML | `lxml.iterparse` | streaming dell'export AH |
| CLI | **Typer** (`askesis …`) | unica interfaccia per le skill |
| Migrazioni | SQL numerate + tabella `schema_migrations` | portabile su Swift |
| Test | pytest, **Hypothesis** (property-based), golden file su **dati sintetici** | correttezza delle metriche |
| Qualità | Ruff, mypy (strict sul core) | |
| Report | Markdown + grafici (matplotlib/plotly) → poi dashboard | |
| Evidenza/regole/config | YAML/TOML sotto git | revisionabili |
| API locale (fase B iOS / dashboard) | FastAPI | solo quando serve |
| iOS (futuro) | Swift, SwiftUI, HealthKit | |

---

## 17. Directory structure

```
askesis/
├─ AGENTS.md                     # regole always-on, safety, confini di ruolo
├─ README.md
├─ pyproject.toml
├─ docs/
│  ├─ architecture.md            # questo documento
│  ├─ roadmap.md                 # piano fasi MVP (sostituisce §23)
│  └─ adr/                       # ADR-001…
├─ schemas/v1/                   # JSON Schema generati (contratto di ingestion)
├─ migrations/                   # 0001_init.sql …
├─ src/askesis/
│  ├─ core/        (time, units, ids, fingerprint)
│  ├─ model/       (entità Pydantic: raw, plan, reference, derived, state, decision, intervention, evidence)
│  ├─ store/       (db, repository, query bitemporali, viste resolved)
│  ├─ ingestion/   (pipeline, dedup, conflict)
│  │  └─ adapters/ (manual, apple_health_export, csv_generic, healthkit_sync, strava)
│  ├─ quality/     (regole, scoring)
│  ├─ analytics/   (registry, body/, strength/, running/, recovery/, concurrent/)
│  ├─ state/
│  ├─ safety/
│  ├─ plan/        (programmi, target, progression rule engine)
│  ├─ decisions/   (catalogo problemi, opzioni, scoring, validatore)
│  ├─ interventions/
│  ├─ evidence/    (loader, validatore, verifica DOI)
│  ├─ reporting/
│  └─ cli/
├─ knowledge/
│  ├─ evidence/{sources,claims,parameters}/*.yaml
│  ├─ rules/{progression,problems,safety,dq}/*.yaml
│  └─ reference/{exercises,zone_models}/*.yaml
├─ config/                       # default di sistema (priorità sorgenti, soglie)
├─ <cartella skill dell'assistente>/<skill>/  # SKILL.md + references/ (locale)
├─ tests/{unit,golden,property,fixtures_synthetic}/
├─ data/          (GITIGNORED)   # coach.db, inbox/, archive/raw/, backups/, athlete_overrides
├─ reports/       (GITIGNORED)
└─ ios/           (futuro)
```

---

## 18. Dependency graph

```
core ◄── model ◄── store
                     ▲
ingestion ──► quality ──► store
analytics ──► store, quality, knowledge/reference, evidence.parameters
state     ──► analytics(outputs), plan, store
safety    ──► state, analytics, knowledge/rules/safety
decisions ──► state, safety, evidence, plan(read), knowledge/rules/problems
interventions ──► decisions, analytics, state, plan(write via versioning)
plan      ──► model, store, reference           (scritture solo da interventions)
evidence  ──► model (NESSUNA dipendenza dai dati atleta)
reporting ──► state, analytics, decisions, interventions (read-only)
cli       ──► tutto
skills    ──► cli (mai import diretti, mai accesso a data/)
```

Regole: nessuna dipendenza verso l'alto (es. analytics → decisions vietato); `evidence` isolato; l'unico
writer di PLAN è `interventions` (+ `rule_execution` per L1); un test architetturale verifica gli import.

---

## 19. Example data flow (simbolico)

1. **Mattina:** pesata su bilancia smart → Apple Health; più tardi export in `inbox/`.
2. `askesis ingest inbox/export.zip` → hash archiviato, batch creato → parser streaming → normalizzazione (unità → kg, offset → UTC + `tz` + `local_date`) → validazione → la pesata manuale dello stesso giorno già presente finisce in un `duplicate_group`; vince la bilancia per priorità → commit RAW → soft DQ: una corsa con HR media sospetta (campioni mancanti) → flag.
3. **Sera:** l'atleta detta in chat la sessione di pesi → skill `health-data` la traduce in comando CLI → set validati (una riga senza RPE resta `proximity_unknown`).
4. Ricalcolo incrementale per `local_date` e finestre successive: `weight_ema`, `weight_rate_14`, `hard_set`, `volume_per_muscle_week`, `e1rm_session`, `srpe_load`, `spacing` (corsa del mattino + sessione lower della sera < soglia → violazione registrata).
5. Nuovo `state_snapshot` (`knowledge_cutoff` = ora).
6. Safety: nessun flag. La violazione di spacing alimenta `P-CONC-01` solo se ricorrente (regola: ≥ N volte in 14 gg).
7. Report giornaliero L0 segnala spacing e il campo con DQ ridotto.

---

## 20. Example decision flow (simbolico)

Contesto: fase dichiarata `fat_loss`; review settimanale.

1. **OBSERVE:** snapshot S_t, cutoff t.
2. **ANALYZE:** `weight_rate_28` con IC che include 0; `intake_mean` DQ B; `waist_trend` negativa ma entro la TEM; aderenza training alta; `context_event` "viaggio" nella finestra.
3. **IDENTIFY:** scatta `P-BODY-01` (rate sotto target di fase). Gating DQ superato (≥ B).
4. **GENERATE OPTIONS:** O1 nessun cambio + finestra estesa (viaggio come confondente); O2 audit del logging (DQ: completezza/giorni identici?); O3 riduzione del target energetico di ΔE, con ΔE dal TDEE adattivo; O4 aumento dell'attività (passi/corsa easy) → valutata con `concurrent-training`.
5. **EVALUATE:** safety: rate non eccessivo, nessun flag EA → O3/O4 ammesse. O4 tocca il budget di fatica → penalizzata da concurrent-training. O1 vince sull'attribuibilità perché c'è un confondente aperto.
6. **PROPOSE:** decisione = O1 + O2 (misura prima di intervenire), confidence `moderate` (DQ B, segnale debole), expected outcome: con log auditato e 14 gg senza confondenti l'IC si restringe abbastanza da decidere; reassessment t + 14 gg.
7. **Atleta approva** → intervento `measurement_protocol` attivo, pre-registrato.
8. **REASSESS** a t + 14: se `weight_rate` resta sotto target con DQ ≥ B → nuova decisione che propone O3, collegata alla precedente.

---

## 21. Risks

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Overengineering per N=1 | il progetto non arriva all'uso | fasi con valore utilizzabile presto; YAGNI su DuckDB/API/iOS |
| **Carico di logging** → calo di aderenza | dati scarsi, abbandono | grammatica di log minima, import automatici, campi opzionali, modalità low-tracking |
| Falsa precisione | decisioni sbagliate con sicurezza | incertezza obbligatoria, gating DQ, confidence = min |
| Allucinazione numerica dell'LLM | perdita di fiducia | ADR-004 + validatore dei riferimenti |
| Limiti dell'inferenza N-of-1 (regressione alla media, confondenti, campioni piccoli) | conclusioni spurie | pre-registrazione, una variabile alla volta, linguaggio "compatibile con", Response Profile separato |
| Validità dei wearable (calorie, fasi sonno, SDNN, VO2max) | segnali rumorosi | tier di precisione per sorgente, serie omogenee, flag ai cambi device |
| Falsi negativi/positivi della safety | danno o alert ignorati | tier, soglie conservative, audit, revisione periodica delle regole |
| Privacy di dati sanitari | esposizione | local-first, minimizzazione verso l'LLM, `data/` fuori da git, backup cifrati |
| Schema churn | migrazioni fragili | versioning e upcaster fin dal primo giorno, test di migrazione |
| Attivazione accidentale di `personal-trainer` | seconda fonte di verità | isolamento in fase 0 |
| Perdita di dati | irreversibile | backup automatici, archivio dei file originali |
| Evidenza obsoleta | raccomandazioni superate | `review_due`, stato `contested` |

---

## 22. Open questions

Risposte registrate in `docs/roadmap.md` (2026-10-03). Elenco originale:

1. Isolamento di `personal-trainer`: meccanismo.
2. Sorgenti reali (Watch, bilancia, app nutrizione, app pesi, fascia, Strava).
3. Gerarchia degli obiettivi e gare.
4. Tolleranza al logging (RPE/RIR, nutrizione pesata/stimata, frequenza misure vita).
5. Dati sensibili opzionali; sesso per le formule.
6. Flusso di approvazione e ampiezza di L1.
7. Cadenze: giorno di review, inizio settimana, cutoff del giorno nutrizionale.
8. Lingua, fuso orario, viaggi.
9. iOS: obiettivo reale o da tenere aperto.
10. Dashboard o report markdown.
11. Contesto medico.
12. Backup.

---

## 23. Recommended implementation phases

> **SOSTITUITO** dal piano MVP in `docs/roadmap.md`. Motivo: il valore del sistema sta nello storico,
> quindi i dati reali entrano dal primo giorno (il RAW append-only e versionato lo rende sicuro); l'MVP è
> una fetta verticale utilizzabile al più presto. Il piano originale (dati reali in fase 7) è ritirato.
