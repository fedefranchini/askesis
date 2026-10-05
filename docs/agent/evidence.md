# Evidenze

Dettagli richiamati da `AGENTS.md` (regola 5).


Le scelte metodologiche (esercizi, volume, intensità, progressione, deload, corsa, concurrent training,
target calorici e proteici, ritmo di dimagrimento, soglie di safety, recovery, sonno) devono derivare da
**pubblicazioni ufficiali e consultabili**.

1. **Identificativo verificato.** Ogni claim della evidence KB ha un DOI o PMID **controllato davvero**
   tramite Crossref (`api.crossref.org`) o PubMed (E-utilities), con titolo, autori, anno e rivista
   corrispondenti. Mai inventare o ricostruire citazioni a memoria. Se la verifica non riesce, il claim
   resta `unverified` e va detto esplicitamente all'atleta.
2. **Gerarchia delle fonti:** (a) position stand / linee guida / consensus di enti riconosciuti (ACSM,
   ISSN, IOC, AASM…); (b) revisioni sistematiche e meta-analisi; (c) RCT; (d) altro. Preferire la versione
   pubblicata in rivista a preprint. Per ogni fonte indicare **tipo di studio, popolazione, limiti** e
   segnalare quando la popolazione è poco simile all'atleta.
3. **Evidenza debole, contrastante o assente** → dirlo esplicitamente ed etichettare la scelta come
   **"opinione esperta"** o **"preferenza personale"**, separata dalle scelte basate su evidenze.
4. **Ogni raccomandazione cita i claim su cui si basa**, con link consultabile (`https://doi.org/<DOI>` o
   `https://pubmed.ncbi.nlm.nih.gov/<PMID>/`).
5. **Preferenze personali** (esercizi graditi, orari, attrezzatura) sono legittime ma etichettate come tali.

**Copyright e privacy della KB.** La evidence KB è conoscenza generale e può stare nella repo pubblica,
ma contiene **solo** metadati, riassunti scritti con parole proprie ed eventuali citazioni brevissime.
**Mai** PDF o testi integrali: se servono per la lettura vanno in `private/literature/` (esclusa da git).
Il **collegamento tra evidenze e dati dell'atleta** (interventi, decisioni, override personali) resta
**solo** nei file privati.

**Verifica periodica.** `python -m askesis.kb_verify [file …]` ricontrolla ogni DOI su Crossref e ogni PMID su
PubMed (titolo, primo autore, anno rispetto alla citazione salvata); in CI gira ogni lunedì
(`.github/workflows/kb-verify.yml`). Le fonti private si verificano in locale passando il file come argomento.
