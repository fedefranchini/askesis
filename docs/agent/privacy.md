# Privacy e repository pubblica

Dettagli richiamati da `AGENTS.md` (regola 6).


Questa repository è **pubblica**. I dati personali e sanitari dell'atleta non devono mai entrarci.

1. **Dati personali solo in file privati**: `data/` (staging, database, report, backup, foto, export),
   `private/` (profilo, onboarding, contesto medico, interventi, roadmap personale, evidenze su temi
   personali) e nelle istruzioni locali private dell'assistente. Le risposte all'onboarding e ogni dato dell'atleta vanno **solo** lì.
   Docs, codice, test ed esempi pubblici usano dati sintetici o segnaposto.
2. **Nei file pubblici solo strutture generiche.** Ogni dato personale, anche futuro, compare nei file
   pubblici solo come struttura generica (es. una variabile di contesto personalizzabile), mai con il suo
   nome specifico. Lo stesso vale per gli **argomenti**: se la sola presenza di un tema (nella KB, nei
   formati di logging, negli esempi) rivela qualcosa sull'atleta, quel tema va nei file privati.
3. **Prima di ogni commit e di ogni push** verificare che nessun dato personale stia entrando o uscendo:
   rivedere file e contenuto aggiunto (nomi, età, misure, date personali, calendario, abitudini, salute,
   percorsi locali della home, email, token). Gli hook `.githooks/pre-commit` e `.githooks/pre-push`
   (con la denylist privata `private/denylist.txt`) sono una rete di sicurezza, non un sostituto della
   revisione. Quando emerge un nuovo dato personale sensibile, aggiungerne i termini alla denylist.
   **Mai** usare `--no-verify`.
4. **Nessuna attribuzione all'assistente IA** nei commit e nelle PR (nessun trailer `Co-Authored-By`, nessuna
   riga "generated with …"): l'autore è sempre l'atleta/proprietario della repo. Disattivato anche nelle
   impostazioni locali dell'assistente e verificato dall'hook `commit-msg`; l'autore e il committer devono
   usare l'indirizzo noreply di GitHub (controllo negli hook).
5. **Esempi lontani dal caso reale.** Esempi, test, help e docstring usano valori coerenti con il profilo
   fittizio dei test (`tests/synthetic.py`), mai valori vicini ai dati reali dell'atleta (peso, intake,
   misure, età, fuso, regioni del corpo con problemi noti). Prima di ogni commit controllare anche i
   valori di esempio, non solo la denylist. Rete di sicurezza: `privacy-guard` passa le righe aggiunte a
   `.githooks/real_values.py`, che blocca i numeri dentro gli intervalli reali dell'atleta (regole private in
   `private/real_values.txt`: intervallo, solo decimali, contesto); esclusi `knowledge/` (valori pubblicati), file di terze
   parti in `vendor/` e il lock delle dipendenze.
   Un falso positivo verificato si accetta con il marcatore `real-values: ok` sulla riga. Aggiornare le regole
   quando cambiano i valori reali o il piano.
6. **Riscrittura dello storico** (con force push) **solo** se un contenuto pubblicato rivela un dato reale
   dell'atleta o lo identifica. Le coincidenze con esempi generici non la richiedono; nei nuovi file si
   usano comunque esempi lontani dal caso reale.
7. Commit **solo su richiesta o approvazione esplicita**. **L'approvazione di un commit include il push**,
   salvo diversa indicazione; il controllo privacy prima del push resta obbligatorio.

## Esportazione completa

`bin/ak export [--out DIR] [--format both|json|csv]` scrive JSON e CSV solo in cartelle private: per
impostazione predefinita `<cartella del database>/exports/<data_ora>/`; `--out` è ammesso solo dentro la cartella
del database, dei backup o `private/` (percorso risolto, symlink inclusi). Il codice rifiuta (exit 1, nulla
scritto) cartelle sincronizzate con un cloud (iCloud Drive, Dropbox, Google Drive, OneDrive, File Provider),
cartelle dentro la repository non ignorate da git e cartelle già esistenti. Cartella 0700, file 0600; a schermo
solo percorso, righe per file e hash del manifest. Trasferimento solo via AirDrop, mai iCloud Drive, mail o altri cloud.

## Skill

La skill esterna `personal-trainer` è disattivata (spostata fuori dalla cartella delle skill attive): solo
riferimento in lettura.
