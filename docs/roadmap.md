# Askesis — Roadmap MVP

> Sostituisce §23 di `docs/architecture.md`.
> Principio: **i dati reali entrano dal primo giorno.** Il valore sta nello storico; il RAW append-only e
> versionato rende sicuro registrare prima che il software sia completo.
>
> Versione pubblica. Le decisioni specifiche dell'atleta (priorità, abitudini di logging, calendario,
> parametri personali) vivono in `private/roadmap.personal.md`, escluso da git.

## Decisioni di sistema (default configurabili)

| Tema | Default |
|---|---|
| Skill `personal-trainer` | Disattivata (spostata in `a disabled-skills folder`), usata solo come riferimento |
| Logging | Peso giornaliero a digiuno; energia + proteine giornaliere (stimate da app); RIR sulle serie di lavoro (se non ricordato → vuoto, mai inventato); vita settimanale con **2–3 letture** (stima TEM); giorni incompleti marcati `partial` |
| Approvazione | "approvo" esplicito in chat, registrato. **L1** = solo doppia progressione con parametri scritti nella versione di programma + deload già pianificati. Deload reattivi = L2 |
| Cadenza | Review settimanale **il lunedì mattina** sulla settimana lun–dom appena chiusa; chiusura del giorno nutrizionale configurabile (es. 03:00) |
| Configurazione personale | Lingua, fuso, sesso per le formule, sorgenti: in file privati |
| Report | Markdown |
| Screening salute | Tipo PAR-Q+ prima di qualsiasi test massimale |
| Backup | Time Machine su disco esterno **cifrato** + `askesis backup` (snapshot via SQLite online backup API in `data/backups/`, rotazione 7 giornalieri + 4 settimanali). DB vivo **mai** in iCloud Drive; snapshot in iCloud solo con Protezione avanzata dei dati o dentro immagine disco cifrata |
| Privacy | Dati personali solo in `data/`, `private/`, `assistant local instructions file` (esclusi da git, protetti da hook) |

Le soglie di safety in `AGENTS.md` sono default provvisori `[PARAM]`, da verificare in F4.

## Timeline

```
Settimana 1 : F0 (setup) ─► Onboarding + Intervento n.1 ─► Settimana di test (baseline)
              F1 (data core + CLI) in parallelo
Settimana 2 : F1 chiusa (import staging) · F1b (Comando Rapido) · F2 avviata
Settimana 3 : F2 chiusa (prima review con metriche) · F3
Settimana 4 : F3 chiusa → MVP completo · prima retrospettiva mensile a fine mese
```

## Fasi

### F0 — Setup · 1 sessione
Ordine vincolante: git init + `.gitignore` **prima** di qualsiasi dato personale → `AGENTS.md` → roadmap.
- Isolamento della skill `personal-trainer`
- git init, `.gitignore`, hook di protezione privacy, attribuzione disattivata
- `AGENTS.md` pubblico (regole di sistema) + `assistant local instructions file` privato (dati personali)
- Setup backup

### Onboarding + Intervento n. 1 · 1–2 sessioni
- Intervista a blocchi (max 5–6 domande per volta): dati base e sorgenti; salute (screening PAR-Q+);
  allenamento con i pesi attuale e passato; corsa; calendario personale (studio/lavoro) e disponibilità;
  attrezzatura; infortuni; sonno; alimentazione; storico peso; passi medi.
- Riepilogo del profilo → conferma dell'atleta → registrazione **solo in file privati**.
- **Evidence KB minima** (vedi sotto) completata **prima** della proposta dell'intervento n. 1.
- **Intervento n. 1** (pre-registrato, richiede "approvo"), che contiene:
  - fase di dimagrimento con **criteri di uscita** (vita, foto, forza, durata massima) e **fase successiva prevista** (mantenimento → eventuale massa);
  - target calorico/proteico iniziale **basato sul prior** (formula), con incertezza ampia e rivalutazione pre-registrata a ~3–4 settimane, quando il TDEE adattivo diventa usabile;
  - programma di allenamento con parametri di doppia progressione e deload pianificati;
  - **periodi ad alto carico esterno** (es. sessioni d'esame) pianificati in anticipo come blocchi a carico ridotto (es. calorie di mantenimento, volume ridotto);
  - **"settimana minima"** di riserva per le settimane difficili;
  - passi giornalieri: baseline e, se opportuno, target come leva.

### Evidence KB minima · 1–2 sessioni · prima dell'intervento n. 1
Anticipa una parte di F4. **Ogni parametro del primo piano deve essere collegato a claim con DOI/PMID
verificato** (Crossref/PubMed); ciò che resta senza evidenza è etichettato "opinione esperta" o
"preferenza personale". Formato: YAML in `knowledge/evidence/` (solo metadati e riassunti propri; mai PDF
o testi integrali). Per ogni fonte: tipo di studio, popolazione, limiti, certezza, applicabilità.

Fonti candidate. Lo stato "ID verificato" indica solo che DOI/PMID esiste e corrisponde a titolo, autore,
anno e rivista (pre-check del 2026-10-03); lettura critica, estrazione dei claim e grading sono il lavoro
di questa fase.

| Area | Fonte | Tipo | Identificativo | ID verificato |
|---|---|---|---|---|
| Screening pre-esercizio | Riebe et al., 2015 — ACSM preparticipation screening | position | [10.1249/MSS.0000000000000664](https://doi.org/10.1249/mss.0000000000000664) | ✅ Crossref |
| Screening pre-esercizio | Warburton et al., 2013 — PAR-Q+ / ePARmed-X+ (Can Fam Physician) | strumento | [PMID 23486800](https://pubmed.ncbi.nlm.nih.gov/23486800/) | ✅ PubMed (senza DOI) |
| Energia, ritmo di dimagrimento | Thomas et al., 2016 — ACSM/AND/DC Nutrition and Athletic Performance | position | [10.1249/MSS.0000000000000852](https://doi.org/10.1249/MSS.0000000000000852) | ✅ PubMed |
| Energia, ritmo di dimagrimento | Aragon et al., 2017 — ISSN: diets and body composition | position | [10.1186/s12970-017-0174-y](https://doi.org/10.1186/s12970-017-0174-y) | ✅ Crossref |
| Energia, ritmo di dimagrimento | Helms, Aragon & Fitschen, 2014 — natural bodybuilding contest prep | review | [10.1186/1550-2783-11-20](https://doi.org/10.1186/1550-2783-11-20) | ✅ Crossref |
| Energia, ritmo di dimagrimento | Garthe et al., 2011 — two weight-loss rates in elite athletes | RCT | [10.1123/ijsnem.21.2.97](https://doi.org/10.1123/ijsnem.21.2.97) | ✅ Crossref |
| Energia, ritmo di dimagrimento | Hall et al., 2011 — energy imbalance and bodyweight (Lancet) | modello | [10.1016/S0140-6736(11)60812-X](https://doi.org/10.1016/s0140-6736(11)60812-x) | ✅ Crossref |
| Energia, ritmo di dimagrimento | Mifflin et al., 1990 — REE equation (prior) | validazione | [10.1093/ajcn/51.2.241](https://doi.org/10.1093/ajcn/51.2.241) | ✅ Crossref |
| Ricomposizione | Barakat et al., 2020 — body recomposition | review narrativa | [10.1519/SSC.0000000000000584](https://doi.org/10.1519/ssc.0000000000000584) | ✅ Crossref |
| Proteine | Jäger et al., 2017 — ISSN: protein and exercise | position | [10.1186/s12970-017-0177-8](https://doi.org/10.1186/s12970-017-0177-8) | ✅ Crossref |
| Proteine | Morton et al., 2018 — protein supplementation MA | SR/MA | [10.1136/bjsports-2017-097608](https://doi.org/10.1136/bjsports-2017-097608) | ✅ Crossref + PubMed |
| Pesi: progressione | ACSM, 2009 — progression models in resistance training | position | [10.1249/MSS.0b013e3181915670](https://doi.org/10.1249/mss.0b013e3181915670) | ✅ Crossref |
| Pesi: volume/frequenza | Pelland et al., 2025/26 — RT dose-response meta-regressions (Sports Med) | MA | [10.1007/s40279-025-02344-w](https://doi.org/10.1007/s40279-025-02344-w) | ✅ Crossref + PubMed |
| Pesi: volume | Schoenfeld, Ogborn & Krieger, 2017 — volume dose-response | MA | [10.1080/02640414.2016.1210197](https://doi.org/10.1080/02640414.2016.1210197) | ✅ Crossref |
| Pesi: frequenza | Schoenfeld, Ogborn & Krieger, 2016 — training frequency | MA | [10.1007/s40279-016-0543-8](https://doi.org/10.1007/s40279-016-0543-8) | ✅ Crossref |
| Pesi: carico | Schoenfeld et al., 2017 — low vs high load | MA | [10.1519/JSC.0000000000002200](https://doi.org/10.1519/jsc.0000000000002200) | ✅ Crossref |
| Pesi: vicinanza al cedimento | Robinson et al., 2024 — proximity to failure dose-response | MA | [10.1007/s40279-024-02069-2](https://doi.org/10.1007/s40279-024-02069-2) | ✅ Crossref |
| Pesi: vicinanza al cedimento | Refalo et al., 2023 — proximity to failure and hypertrophy | MA | [10.1007/s40279-022-01784-y](https://doi.org/10.1007/s40279-022-01784-y) | ✅ Crossref |
| Pesi: RIR/RPE | Helms et al., 2016 — RIR-based RPE scale | review | [10.1519/SSC.0000000000000218](https://doi.org/10.1519/ssc.0000000000000218) | ✅ Crossref |
| Deload | Bell et al., 2023 — deloading, Delphi consensus | consenso esperti | [10.1186/s40798-023-00633-0](https://doi.org/10.1186/s40798-023-00633-0) | ✅ Crossref (evidenza debole) |
| Concurrent | Schumann et al., 2022 — concurrent training, updated MA | MA | [10.1007/s40279-021-01587-7](https://doi.org/10.1007/s40279-021-01587-7) | ✅ Crossref |
| Concurrent | Wilson et al., 2012 — interference meta-analysis | MA | [10.1519/JSC.0b013e31823a3e2d](https://doi.org/10.1519/jsc.0b013e31823a3e2d) | ✅ Crossref |
| Corsa: intensità | Seiler, 2010 — training intensity distribution | review | [10.1123/ijspp.5.3.276](https://doi.org/10.1123/ijspp.5.3.276) | ✅ Crossref |
| Corsa: talk-test | Persinger et al., 2004 — consistency of the talk test (MSSE) | studio | [PMID 15354048](https://pubmed.ncbi.nlm.nih.gov/15354048/) | ✅ PubMed (senza DOI) |
| Corsa: talk-test | Reed et al., 2014 — the talk test (Curr Opin Cardiol) | review | [10.1097/HCO.0000000000000097](https://doi.org/10.1097/HCO.0000000000000097) | ✅ PubMed |
| Carico | Foster et al., 2001 — session-RPE | studio | [10.1519/00124278-200102000-00019](https://doi.org/10.1519/00124278-200102000-00019) | ✅ Crossref |
| Carico | Impellizzeri et al., 2020 — ACWR pitfalls | critica metodologica | [10.1123/ijspp.2019-0864](https://doi.org/10.1123/ijspp.2019-0864) | ✅ Crossref |
| Safety: REDs | Mountjoy et al., 2023 — IOC consensus on REDs | consensus | [10.1136/bjsports-2023-106994](https://doi.org/10.1136/bjsports-2023-106994) | ✅ PubMed |
| Sonno | Watson et al., 2015 — AASM/SRS recommended sleep | consensus | [10.5665/sleep.4716](https://doi.org/10.5665/sleep.4716) | ✅ Crossref |
| Sonno | Walsh et al., 2021 — sleep and the athlete, expert consensus | consensus | [10.1136/bjsports-2020-102025](https://doi.org/10.1136/bjsports-2020-102025) | ✅ Crossref |
| Recovery: HRV | Plews et al., 2013 — HRV monitoring in endurance athletes | review | [10.1007/s40279-013-0071-8](https://doi.org/10.1007/s40279-013-0071-8) | ✅ Crossref |
| Integratori | Kreider et al., 2017 — ISSN: creatine | position | [10.1186/s12970-017-0173-z](https://doi.org/10.1186/s12970-017-0173-z) | ✅ Crossref |

Aree senza fonte di primo livello ancora individuata (da cercare o etichettare come opinione esperta):
passi giornalieri come leva per il dimagrimento, criteri di uscita dalla fase di dimagrimento,
protocollo per le foto, soglie numeriche di dolore/infortunio per la safety.

### Settimana di test (baseline) · settimana 1 del piano
Prerequisito: screening salute pulito; altrimenti solo test submassimali o dopo valutazione medica.
- **Corsa** — protocollo standardizzato:
  1. *Corsa facile con talk-test*: passo al quale si riescono a dire frasi complete (≈ sotto la prima
     soglia ventilatoria), pause di cammino ammesse; registrare passo, FC, tempo/distanza e motivo di
     eventuale stop. Stima il passo aerobico facile e permette di testare ipotesi sul limite di distanza
     attuale (es. "mi fermo perché corro troppo veloce").
  2. *Prova cronometrata breve* (es. 3 km dopo riscaldamento), ≥ 48 h dopo e solo se lo screening lo
     consente: benchmark ripetibile ogni 6–8 settimane. Non stima una FCmax vera.
  - Zone FC iniziali dal talk-test, marcate provvisorie; formule basate sull'età solo come ultima risorsa (DQ bassa).
- **Pesi** — sui fondamentali, salire fino a una serie da 5–8 ripetizioni a **RIR noto (~2)**, mai a
  cedimento → baseline e1RM. La settimana 1 del programma è anche calibrazione.
- **Vita** — 2–3 letture con protocollo fisso (punto anatomico, mattina, a digiuno, fine espirazione).
- **Foto standardizzate** — stessa luce, ora, distanza, posa (fronte/lato/retro); solo in `data/`.
- **FC a riposo** — dato del wearable + facoltativa misura manuale al risveglio (60 s, sdraiato).

### Guida Apple Watch · dopo la settimana di test
Documento in `docs/` (nessun dato personale): **zone FC personalizzate** dal test; fitness
cardiorespiratorio (VO2max stimato); monitoraggio del sonno; notifiche di FC alta/bassa e ritmo
irregolare; metriche di corsa (disponibilità dipendente dal modello); permessi di condivisione in Salute.

### F1 — Data core + CLI · 2–3 sessioni · 3–5 giorni
uv/pyproject; SQLite + migrazioni; envelope; `units`, `time` (fuso e cutoff configurabili); entità MVP
(`body_weight`, `body_measurement`, `nutrition_day`, `training_session` + `set_record`,
`running_session`, `daily_activity` **(passi)**, `sleep_session`/`resting_hr_daily` opzionali,
`subjective_checkin` minimo, `test_result`, `context_event`, `health_event`, `athlete_attribute`, `goal`);
DQ hard; supersession/retraction; ingest idempotente; catalogo esercizi minimo.
CLI: `askesis log weight|food|waist|gym|run|steps|checkin`, `askesis fix`, `askesis show day|week`,
`askesis import-staging`, `askesis backup`.
**Uscita:** logging giornaliero cronometrato < 2 min; staging importato senza perdite; test verdi.

### F1b — Riduzione attrito: Comando Rapido iOS · 1 sessione
- Comando Rapido "Peso" (un tocco): legge l'ultimo peso da Salute se la bilancia vi scrive, altrimenti lo
  chiede → scrive un file JSON conforme all'envelope v0 in una cartella iCloud Drive dedicata.
- Comando "Fine giornata" (opzionale): passi del giorno da Salute + energia/proteine se l'app di
  nutrizione scrive su Salute.
- `askesis import-inbox`: importa e archivia i file (idempotente). In iCloud solo piccoli file di inbox, mai il database.

### F2 — Metriche + review settimanale · 2–3 sessioni · ~1 settimana
Registro metriche; `weight_ema`, `weight_rate`, `intake_mean`, `adaptive_tdee` (prior + incertezza),
`hard_set`, `volume_per_muscle_week`, `e1rm`, volume settimanale di corsa, **media passi settimanale**;
grade DQ semplificato; test golden e property-based su dati sintetici; `askesis review --week`.
**Uscita:** rebuild riproducibile; ogni numero della review rimanda a una metrica.

### F3 — Piano + interventi + safety codificata · 2 sessioni · ~1 settimana
`phase_declaration` (con criteri di uscita e fase successiva), `programme_version` (inclusa la variante
**settimana minima**), `nutrition_target_version`, **periodi a carico ridotto** pianificati;
registro interventi (pre-registrazione, freeze, emendamenti, valutazione calcolata); record decisionale
minimo con "approvo"; L1 come `rule_execution`; 4 regole di safety codificate con tier (perdita troppo
rapida, dolore/infortunio, sintomi cardiovascolari, segnali di disturbo alimentare), visibili al log e in
review; **passi** come leva d'intervento; **retrospettiva mensile** (`askesis review --month`) che alimenta
un **Athlete Response Profile** minimo; import intervento n. 1.
**Uscita:** "perché abbiamo cambiato X e cosa è successo" risponde via query; safety testata su casi sintetici.

**MVP completo: ~6–8 sessioni, 2–3 settimane di calendario.**

## Fasi successive (ordine indicativo)

| Fase | Contenuto | Stima |
|---|---|---|
| F4 | Evidence KB essenziale: prima i `[PARAM]` usati dall'MVP (incluse soglie di safety), poi lacune (running, concurrent, REDs) | 1–2 sessioni |
| F5 | Apple Health export (peso, sonno, FC a riposo, passi, corse con FC) + metriche running/recovery | 2–3 |
| F6 | State model + safety estesa (EA/REDs, fatica, malattia) | 2 |
| F7 | Decision engine (catalogo problemi, scoring) | 2–3 |
| F8 | Skill modulari (health-data, intervention-management, orchestrator per primi) | 2 |
| F9 | Dashboard / iOS | aperto |
