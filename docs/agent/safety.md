# Safety

Regole complete; l'essenziale è in `AGENTS.md` (regola 1). Il codice le applica in
`src/askesis/safety/` con soglie in `knowledge/parameters/plan_safety.yaml`.


L'assistente non è un medico: descrive pattern e raccomanda valutazioni, **non fa diagnosi**.

| Segnale | Azione |
|---|---|
| **Sintomi cardiovascolari**: dolore/oppressione al petto, svenimento o quasi durante lo sforzo, palpitazioni con capogiro, fiato corto sproporzionato, notifiche di ritmo irregolare | **STOP**: interrompere l'allenamento; sintomi acuti → 112; altrimenti valutazione medica prima di riprendere. Nessun test massimale finché non chiarito |
| **Dolore / infortunio**: dolore ≥ 4/10 durante un esercizio, dolore che peggiora o persiste > 1 settimana, gonfiore, perdita di funzione | Fermare/sostituire il movimento interessato; dolore persistente o acuto → medico/fisioterapista. Registrare `health_event` dopo conferma |
| **Perdita di peso troppo rapida**: trend > 1% del peso/settimana per ≥ 2 settimane (esclusa la prima settimana di dieta) | Segnalare, non aumentare il deficit, proporre correzione verso l'alto |
| **Segnali di disturbo alimentare**: restrizione rigida o crescente, abbuffate/compensazioni, angoscia per log o sessioni saltate, esercizio usato per "compensare" | Non stringere i target; linguaggio non giudicante; offrire di ridurre il tracking; suggerire un professionista |
| Malattia dichiarata dall'atleta (`bin/ak log ill …` o «Non sto bene oggi» nella dashboard; flag T1 `safety.illness@1` finché l'episodio è aperto) | Con febbre o sintomi sotto il collo niente allenamento fino a risoluzione; altrimenti niente allenamento intenso. Gli interventi che aumentano lo stress (anche il taglio calorico) restano bloccati; la fase non viene sospesa. I giorni di malattia sono un confondente dichiarato: sedute previste scusate, baseline del questionario senza quei giorni, commento della seduta e review lo segnalano. Si chiude con «Sto meglio» (`bin/ak log recovered`) |
| **Fame alta persistente con stanchezza alta e umore basso in deficit** (questionario del mattino: almeno 4 giorni su 7 in fase di dimagrimento, solo con la baseline personale completa; flag T1 `safety.reds_pattern@1`) | Segnale da osservare, **non una diagnosi né un giudizio**: non aumentare il deficit, parlarne con l'atleta (un aumento dell'apporto è una proposta da approvare); se continua o compaiono altri sintomi → medico o dietista sportivo. Base: consenso IOC sulla REDs (`safety.reds`, `safety.reds_mental_health`); la combinazione di risposte è una scelta tecnica |

Mai moralizzare sul cibo. Mai premiare restrizioni estreme.
