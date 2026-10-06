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
| Skill `personal-trainer` | Disattivata (spostata fuori dalle skill attive), usata solo come riferimento |
| Logging | Peso giornaliero a digiuno; energia + proteine giornaliere (stimate da app); RIR sulle serie di lavoro (se non ricordato → vuoto, mai inventato); vita settimanale con **2–3 letture** (stima TEM); giorni incompleti marcati `partial` |
| Approvazione | "approvo" esplicito in chat, registrato. **L1** (automatico) = solo doppia progressione con parametri scritti nella versione di programma + deload già pianificati. Le regole versionate (es. `knowledge/rules/calorie_adjustment.yaml`, pre-registrata nel primo intervento) quando scattano generano una **proposta L2** da approvare con un "approvo" rapido. Deload reattivi = L2 |
| Modello di coaching | Il coach (assistente IA) legge i dati, produce i report (giornaliero se rilevante, review del lunedì, retrospettiva mensile) e propone modifiche; il codice calcola; l'atleta decide. Vedi `docs/agent/coaching.md` |
| Rilettura | Pesi, corsa e import: anteprima compatta e salvataggio solo dopo conferma (`--yes`); peso, cibo e valori semplici: salvati subito con rilettura |
| Cadenza | Review settimanale **il lunedì mattina** sulla settimana lun–dom appena chiusa; chiusura del giorno nutrizionale configurabile (es. 03:00) |
| Configurazione personale | Lingua, fuso, sesso per le formule, sorgenti: in file privati |
| Report | Markdown |
| Screening salute | Tipo PAR-Q+ prima di qualsiasi test massimale |
| Backup | Time Machine su disco esterno **cifrato** + `askesis backup` (snapshot via SQLite online backup API in `data/backups/`, rotazione 7 giornalieri + 4 settimanali). DB vivo **mai** in iCloud Drive; snapshot in iCloud solo con Protezione avanzata dei dati o dentro immagine disco cifrata |
| Privacy | Dati personali solo in `data/`, `private/` e nelle istruzioni locali private dell'assistente (esclusi da git, protetti da hook) |

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
Ordine vincolante: git init + `.gitignore` **prima** di qualsiasi dato personale → regole dell'assistente → roadmap.
- Isolamento della skill `personal-trainer`
- git init, `.gitignore`, hook di protezione privacy, attribuzione disattivata
- Regole pubbliche dell'assistente (`AGENTS.md`) + istruzioni locali private (dati personali)
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
| Screening pre-esercizio | Bredin et al., 2013 — PAR-Q+ / ePARmed-X+ (Can Fam Physician) | strumento | [PMID 23486800](https://pubmed.ncbi.nlm.nih.gov/23486800/) | ✅ PubMed (senza DOI) |
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
| Corsa: talk-test | Reed & Pipe, 2014 — the talk test (Curr Opin Cardiol) | review | [10.1097/HCO.0000000000000097](https://doi.org/10.1097/HCO.0000000000000097) | ✅ PubMed |
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

### F1 — Data core + CLI · 2–3 sessioni · 3–5 giorni · ✅ completata (2026-10-03)
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

### F2 — Metriche + review settimanale · 2–3 sessioni · ~1 settimana · ✅ completata (2026-10-04)
Registro metriche; `weight_ema`, `weight_rate`, `intake_mean`, `adaptive_tdee` (prior + incertezza),
`hard_set`, `volume_per_muscle_week`, `e1rm`, volume settimanale di corsa, **media passi settimanale**;
grade DQ semplificato; test golden e property-based su dati sintetici; `askesis review --week`.
**Uscita:** rebuild riproducibile; ogni numero della review rimanda a una metrica.

### F3 — Piano + interventi + safety codificata · 2 sessioni · ~1 settimana · ✅ completata (2026-10-04, salvo import dell'intervento n. 1)
`phase_declaration` (con criteri di uscita e fase successiva), `programme_version` (inclusa la variante
**settimana minima**), `nutrition_target_version`, **periodi a carico ridotto** pianificati;
registro interventi (pre-registrazione, freeze, emendamenti, valutazione calcolata); record decisionale
minimo con "approvo"; L1 come `rule_execution`; 4 regole di safety codificate con tier (perdita troppo
rapida, dolore/infortunio, sintomi cardiovascolari, segnali di disturbo alimentare), visibili al log e in
review; **passi** come leva d'intervento; **retrospettiva mensile** (`askesis review --month`) che alimenta
un **Athlete Response Profile** minimo; import intervento n. 1.
**Uscita:** "perché abbiamo cambiato X e cosa è successo" risponde via query; safety testata su casi sintetici.

**MVP completo: ~6–8 sessioni, 2–3 settimane di calendario.**

### F3b — Pre-avvio del primo intervento · ✅ completata (2026-10-05)
- Regole dell'assistente snelle (8 regole essenziali, safety sempre caricata; dettagli in `docs/agent/`)
- Rilettura compatta con nomi italiani del catalogo; anteprima + `--yes` per pesi, corsa e import
- Scheda del giorno in Apple Note (`ak gym-note create/import`): solo esercizi e carichi; parsing tollerante;
  righe illeggibili segnalate e mai indovinate; import idempotente; righe modificate → correzioni
- Controlli nel codice: permessi che negano scritture dirette in `data/` e il client `sqlite3`; nessuna
  prescrizione con flag di safety T2/T3 aperti; CI (Ruff + test) a ogni push
- Regola di aggiustamento calorico come dato versionato (`knowledge/rules/`), validata dai test

### F3c — Settimane 1–2 del primo intervento · ✅ completata (installazione launchd in attesa del permesso)
- Review rimodulata: **fondamentali** (aderenza calorica, proteine, sessioni svolte/pianificate, sonno, passi) →
  **indicatori ritardati** (peso, vita, forza, corsa) → al massimo **3 punti di attenzione** in linguaggio semplice.
  Per ogni metrica "cambiamento reale" o "dentro il rumore", con rumore stimato dai dati e base dichiarata
  (fonti su errore tecnico di misura e cambiamento minimo rilevabile da verificare nella KB).
- Motore della regola calorica L2 (condizioni minime, zona neutra, limiti, proposta con notifica, applicazione con "approvo", annullamento).
- Backup automatico (launchd) + test di ripristino settimanale (integrità, conteggi, digest delle metriche).
  Richiede il permesso esplicito dell'atleta prima dell'installazione.
- Controllo automatico dei valori di esempio (intervalli reali letti da un file privato) e verifica periodica
  di DOI/PMID della KB in CI.

## Fasi future (sola progettazione — nessuna implementazione finché non approvata)

**Principio architetturale: un solo nucleo, tante interfacce.** Il motore (modello dati, store, metriche, regole,
safety, interventi) è unico; CLI, dashboard web, app iPhone e server MCP sono involucri sottili attorno allo stesso
motore, con gli stessi controlli (validazione, rilettura, approvazioni, safety, append-only). **Il modello operativo
è il coach**: l'assistente IA legge i dati, fa i report e propone; il codice calcola e garantisce i vincoli critici.

| Ordine | Fase | Voce |
|---|---|---|
| 1 | F4 | I — Dashboard web locale + sincronizzazione da Salute |
| 2 | F5 | E — App iPhone nativa con HealthKit |
| 3 | F6 | C — Server MCP locale |
| continuo | — | F — Backlog di precisione · D — Usabilità per chi scarica la repo |
| lungo termine (opzionale) | — | A — Pilota automatico · B — Simulatore · G — Machine learning e dataset |
| vincoli | — | H — Porta aperta a un'app distribuibile |

### F4 · I. Dashboard web locale + sincronizzazione da Salute
**Stato:** F4a (dashboard, avvio automatico con launchd), F4b (import dell'esportazione di Salute:
`bin/ak import-health`) e accesso dall'iPhone tramite Meshnet (solo il telefono autorizzato) completati.
Import di Hevy pronto (`bin/ak import-hevy`), in attesa dell'esportazione. Accesso dall'iPhone: **Meshnet di NordVPN** (decisione
del 2026-10-06). **Tailscale scartato** perché l'atleta usa NordVPN ogni giorno e le due VPN non
risultavano utilizzabili insieme (sull'iPhone, da verificare), mentre Meshnet è integrata nella stessa app (la chiusura di Meshnet annunciata per
dicembre 2025 è stata ritirata da NordVPN a settembre 2025). La dashboard resta indipendente dal fornitore:
ascolta solo sugli indirizzi scelti (`web_bind`, mai jolly), accetta solo il Mac e i client autorizzati
(`web_allowed_clients`) e gli host ammessi (`web_allowed_hosts`); cambiare fornitore = cambiare configurazione. Poi F4b (import storico di Salute) e F4c
(sincronizzazione quotidiana con Comandi Rapidi).
**Dashboard** (localhost, stesso stack Python; involucro sottile attorno al nucleo, nessuna logica di calcolo
duplicata):
- Inserimento con campi pronti: peso, vita, sonno, check-in, contesto, serie in palestra, totali giornalieri di
  energia e proteine. Stessi controlli della CLI: validazione, rilettura/anteprima, safety, append-only.
- Grafici e andamenti: tendenza del peso, TDEE con incertezza, volume per muscolo, e1RM, corsa, passi, aderenza;
  ultima review, stato degli interventi, flag di safety. Librerie grafiche servite in locale (nessuna CDN).
- **Un'unica fonte di verità**: lo stesso database della CLI. Niente fogli di calcolo o documenti come fonte
  dei dati. Gli alimenti restano nell'app di nutrizione: nessun database alimentare interno, solo totali.
- **Accesso dall'iPhone sulla stessa rete Wi-Fi**, con autenticazione (password + cookie di sessione, token per le
  API); nessuna esposizione su internet; HTTPS in rete locale da valutare (certificato locale).
- **Rapporto con la scheda in Note**: la dashboard **affianca**, non sostituisce, la nota della palestra — in
  palestra l'iPhone non è sulla rete di casa e il Mac può essere spento; la nota funziona offline. La dashboard
  serve a casa (inserimenti, consultazione, review).

**Sincronizzazione da Salute** (ordine di lavoro):
1. **Import completo dello storico** dall'esportazione manuale di Salute (`export.zip` → parser in streaming
   dell'XML → envelope), con deduplica e priorità delle sorgenti (docs/architecture.md §15).
2. **Sincronizzazione quotidiana** con un'automazione di Comandi Rapidi che legge i campioni di Salute (sonno,
   passi, FC a riposo, HRV, allenamenti, energia e proteine scritte dall'app di nutrizione) e li invia in JSON alla
   dashboard sulla rete locale, senza cloud.

**Vincoli verificati e punti da verificare sul dispositivo:**
- **iPhone bloccato**: i dati di Salute sono cifrati quando il dispositivo è bloccato e non leggibili in
  background (documentazione Apple, *Protecting user privacy*). Vale per Comandi Rapidi, per app di terze parti e
  per un'app nativa. → L'automazione va agganciata a un momento in cui il telefono è sbloccato (es. fine sveglia,
  apertura di un'app), non a un orario cieco.
- **Cosa legge Comandi Rapidi**: l'azione "Trova campioni di Salute" copre molti tipi (sonno, FC, HRV, passi,
  allenamenti, nutrienti); disponibilità e campi esatti per tipo **da verificare sul dispositivo** con una
  prova per ciascun tipo.
- **Duplicati**: chiave naturale (tipo, inizio, fine, valore, sorgente) e finestre di sovrapposizione; un nuovo
  invio della stessa giornata è idempotente; le correzioni di Salute diventano correzioni (supersessione).
- **Protezione dell'invio**: token segreto per dispositivo, solo rete locale, Mac acceso e raggiungibile; se il
  Mac non risponde l'automazione riprova il giorno dopo (l'import è idempotente).
- **Alternative**: Health Auto Export (app di terze parti; automazioni verso un endpoint REST solo con
  abbonamento; stesso vincolo dell'iPhone bloccato; dati in transito verso un'app esterna) vs **app nativa (F5)**
  (accesso completo con anchored query e gestione delle cancellazioni, ma costo di sviluppo alto). Scelta
  proposta: Comandi Rapidi ora (gratis, nessuna terza parte), app nativa in F5.
- **Costi/benefici**: costo medio; beneficio alto (meno dettatura, dati automatici di sonno e recovery,
  consultazione comoda).

### F5 · E. App iPhone nativa
Invariata nei contenuti (inserimento manuale, scheda del giorno, grafici, review, HealthKit in lettura; adapter
nell'envelope con motore sul Mac; nucleo progettato per poter girare in futuro sul telefono; sincronizzazione
privata con deduplica; review e safety eseguite in automatico sul Mac e consultabili dal telefono; distribuzione
gratuita a 7 giorni o a pagamento, decisione rimandata). Sostituirà la scheda in Note.

### F6 · C. Server MCP locale
Invariato (stato, ultima review, why, prossima sessione, metriche e trend; sola lettura e aggregati di default;
scritture con gli stessi controlli della CLI; prima locale, remoto solo dopo una progettazione di sicurezza;
compatibile con qualsiasi client MCP).
Strumento di sviluppo: la skill `mcp-builder` del repository ufficiale `anthropics/skills`, da installare a
livello di progetto solo dopo audit di `SKILL.md` e script e approvazione dell'atleta.

### F. Backlog di precisione (continuo)
Principio (`AGENTS.md`, regola 8): ogni funzione nuova migliora precisione o aderenza senza aumentare in modo
significativo il carico quotidiano.
- **Protocolli di misura** (settimana di test): pesata standardizzata e verifica mensile della bilancia con un
  peso noto; impostazioni delle macchine; calibrazione periodica del RIR; HRV mattutina con una sessione di
  respirazione di 1 minuto sul Watch; corsa di riferimento ogni 4 settimane a FC fissa; costi/benefici di una
  fascia cardio toracica.
- **Analisi**: filtro di Kalman per peso e TDEE vs metodo a finestre (validazione temporale); effetto del giorno
  della settimana sul peso (Orsama et al. 2014, [10.1159/000356147](https://doi.org/10.1159/000356147),
  identificativo verificato); FC di corsa e temperatura; proiezione della data obiettivo con incertezza.
- **Decisioni**: revisione "avvocato del diavolo" indipendente di ogni intervento; pre-mortem (*opinione
  esperta*); tecniche di aderenza basate su evidenze, es. intenzioni di implementazione (Gollwitzer & Sheeran
  2006, [10.1016/S0065-2601(06)38002-1](https://doi.org/10.1016/s0065-2601(06)38002-1), identificativo verificato,
  contenuto da leggere).

### D. Usabilità per chi scarica la repo (secondario)
Installazione portabile (`uv sync`; CI su Linux verde); configurazione di esempio; formato documentato per piani
scritti a mano e modelli; demo con dati sintetici; disclaimer sanitario chiaro. Principio: funziona senza IA,
funziona meglio con l'IA.

### Backlog a lungo termine (opzionale)
**A. Pilota automatico deterministico.** Regole come dati versionati (già presente il formato:
`knowledge/rules/`); estensione a doppia progressione, deload reattivo con più indicatori concordi, volume di
corsa, settimana minima; isteresi, passi piccoli, tempi minimi, conflitti; prudenza con dati scarsi; modalità
ombra; spiegabilità; registrazione e annullamento. Oggi le regole generano **proposte** (L2), non cambi automatici.

**B. Simulatore di atleti sintetici.** Dinamica del peso da modelli pubblicati (Hall et al. 2011,
[10.1016/S0140-6736(11)60812-X](https://doi.org/10.1016/s0140-6736(11)60812-x), nella KB; Hall 2012,
[10.3945/ajcn.112.036350](https://doi.org/10.3945/ajcn.112.036350), identificativo verificato), rumore, aderenza
variabile, malattie, periodi d'esame, errori di logging; metriche di collaudo delle regole (obiettivo, tempo,
oscillazioni, superamenti, violazioni di safety).

**G. Machine learning e dataset pubblici** (dopo ~1 anno di dati). Un modello complesso entra solo se batte il
semplice in una validazione che rispetta l'ordine temporale. Dataset mai nella repo; licenza, provenienza e uso
commerciale documentati:

| Dataset | Fonte (identificativo verificato) | Uso previsto | Licenza |
|---|---|---|---|
| MyFitnessPal Food Diary | Weber & Achananuparp 2016, [10.1142/9789814749411_0049](https://doi.org/10.1142/9789814749411_0049) | aderenza (simulatore) | da verificare |
| PMData | Thambawita et al. 2020, [10.1145/3339825.3394926](https://doi.org/10.1145/3339825.3394926) | validare metriche | da verificare |
| ScopeSense | Riegler et al. 2023, [10.31219/osf.io/8z5gc](https://doi.org/10.31219/osf.io/8z5gc) (preprint) | idem | da verificare |
| FitRec / Endomondo | Ni et al. 2019, [10.1145/3308558.3313643](https://doi.org/10.1145/3308558.3313643) | risposta FC alla corsa | solo uso accademico |
| OpenPowerlifting | sito del progetto | priori di progressione della forza | pubblico dominio (da confermare) |
| LifeSnaps | Yfantidou et al. 2022, [10.1038/s41597-022-01764-x](https://doi.org/10.1038/s41597-022-01764-x) | recupero, HRV, sonno | da verificare |

### H. Porta aperta a un'app distribuibile (vincoli, nessun lavoro ora)
Nucleo portabile; funzionamento normale senza costi IA per utente; privacy by design (dati sanitari
preferibilmente sul dispositivo); licenze di dataset e componenti tracciate. Da verificare con professionisti
prima di un'eventuale distribuzione: GDPR per dati sanitari; confine con la normativa sui dispositivi medici;
linee guida Apple per app con HealthKit; aspetti fiscali.

### Fasi precedenti riassorbite
Evidence KB → attività continua; export di Salute → F4; state model + safety estesa → F3c/F4 secondo necessità;
decision engine → modello del coach con regole come dati che generano proposte; skill modulari → dopo F6, se utili.
