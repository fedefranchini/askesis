# Decisioni e interventi

Dettagli richiamati da `AGENTS.md` (regola 4).


- Il piano (allenamento, calorie, fase) cambia **solo** con un **"approvo"** esplicito dell'atleta in chat.
- Procedura (CLI, fase F3):
  1. scrivere la pre-registrazione in un file YAML **privato** (`private/interventions/NNNN.yaml`): dominio,
     ipotesi, motivo, cambiamenti di piano, esito atteso (metrica, direzione, intervallo), criteri di
     successo e di stop, aderenza minima, date, claim di evidenza, opinioni esperte e preferenze personali;
  2. `bin/ak intervention propose <file> --title … --category …` e presentare la proposta all'atleta;
  3. solo dopo la risposta: `bin/ak intervention approve N --verbatim "<testo esatto>" --reasoning …`
     (oppure `reject`). Il safety gate può bloccare l'attivazione: non aggirarlo senza motivo esplicito;
  4. emendamenti con `amend`, valutazioni con `evaluate`; spiegazioni con `bin/ak why N`.
- Dopo l'attivazione i campi pre-registrati sono immutabili (garantito dal database).
- Sintomi o infortuni detti in chat: chiedere conferma, poi `bin/ak log event --kind … --flag …`.
- **L1 (senza approvazione)** solo: doppia progressione con parametri scritti nella versione di programma
  attiva, e deload già pianificati. Tutto il resto è L2 (serve "approvo").
- Preferire **una variabile alla volta** per dominio di esito.

## Fonti dei numeri e valori derivati (controllati dal codice)

- **Ogni numero strutturato** della pre-registrazione (contenuti del piano, esito atteso, aderenza minima,
  argomenti dei valori derivati) ha una fonte in `value_sources`, indicizzata per percorso puntato
  (`plan_changes.<nome>.content.…`; vale il prefisso più lungo):
  `claim:<id>` (valore presente nel claim) · `within:claim:<id>:<min>-<max>` (intervallo presente nel claim,
  valore al suo interno) · `param:<id>` · `rule:<id>@<v>` · `expert_opinion: <motivo>` ·
  `personal_preference: <motivo>` · `engineering_choice: <motivo>`. Senza fonte valida
  `intervention propose` non registra la proposta (`--dry-run` per controllarla senza registrarla).
- **Valori derivati** (`derived`): la proposta dichiara formule e data limite dei dati (`data_until`); i
  contenuti del piano li richiamano con `"$derived:<nome>"`. Metodi: `metric` (valore di una metrica al
  `data_until`), `energy_target` (TDEE − deficit per il ritmo dichiarato, con densità energetica da parametro e
  soglia di safety sul metabolismo a riposo), `protein_target` (g/kg × peso di riferimento).
  `propose` mostra i valori **provvisori** con i dati attuali.
- Con valori derivati, `approve` registra solo l'approvazione; `bin/ak intervention activate N` (dalla data di
  avvio, e dopo `data_until`) li calcola con i soli dati fino a `data_until`, li congela nell'evento di
  attivazione (append-only, con impronta) e crea le versioni di piano. Safety gate e controllo "un intervento
  attivo per dominio" vengono ripetuti all'attivazione.

## Livelli di automazione (decisione aggiornata: aggiustamento calorico riportato a L2)

- **L1 (automatico, senza approvazione):** solo doppia progressione con parametri scritti nella versione di
  programma attiva, e deload già pianificati.
- **L2 (serve "approvo"):** tutto il resto. Le regole versionate come dato (es.
  `knowledge/rules/calorie_adjustment.yaml`), pre-registrate nell'intervento, quando le condizioni minime
  sono soddisfatte generano una **proposta** con dati, regola@versione e motivo, che l'atleta approva con un
  "approvo" rapido. Mai proposte di aumento dello stress con flag di safety aperti.
