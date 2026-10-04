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

## Livelli di automazione (decisione 2026-10-04, modifica C6)

- **L1 (senza approvazione, con limiti dichiarati nella versione di piano approvata):** doppia progressione,
  deload pianificati e **regola di aggiustamento calorico** (pilota automatico). La regola calorica si applica
  da sola solo se tutte le condizioni minime sono soddisfatte (aderenza, qualità dati, nessun flag di safety
  aperto), al massimo una volta ogni 2 settimane con il passo dichiarato; ogni applicazione è registrata,
  notificata in una riga e annullabile. Va pre-registrata nell'intervento che la introduce.
- **L2:** tutto il resto, con "approvo".
