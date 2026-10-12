# Server sempre acceso (facoltativo)

Askesis può girare su un piccolo server Linux invece che sul computer di sviluppo. Il server diventa l'unica fonte
di verità; il computer di sviluppo resta per il codice e usa i comandi `ak` sul server via SSH.

## Principi
- **Nessuna porta esposta su internet.** SSH e dashboard solo attraverso la VPN mesh (interfaccia `nordlynx`) e solo
  dai peer elencati (`/etc/askesis/peers`). Firewall `nftables` con politica `drop` in ingresso
  (`deploy/nftables.conf`), più le regole di rete del fornitore.
- **SSH solo con chiavi**, niente root, niente inoltri (`deploy/sshd-askesis.conf`). Tre chiavi distinte:
  - amministrazione (utente con sudo);
  - agente: può eseguire **solo** `ak …`, senza shell (`deploy/ak-gate`, comando forzato con `restrict`);
  - copia dei backup: può solo **leggere** i backup cifrati (`rrsync -ro`).
- **Aggiornamenti di sicurezza automatici** (`unattended-upgrades`, riavvio alle 04:30 se serve).
- **Dati** in `/var/lib/askesis` (cartella `0700` dell'utente di servizio `askesis`). Cifratura del disco: quella
  standard del fornitore.

## Backup
Ogni notte (`askesis-backup.timer`, 02:30): copia online del database → test di ripristino → copia **cifrata con
age** verso le sole chiavi pubbliche dell'atleta (`backup_age_recipients`) → copia esterna con rclone
(`backup_offsite`, solo file nuovi, nulla viene cancellato). Il server non possiede la chiave privata: può scrivere
i backup ma non leggerli. Ogni file cifrato è elencato in `manifest.json` con SHA-256.

Sul computer di sviluppo: `bin/ak backup pull` (rsync dei soli file cifrati, controllo con il manifest) e
`bin/ak backup check-sealed` (decifra in una cartella temporanea privata, integrità, conteggi, data dell'ultimo dato;
la copia in chiaro viene sempre cancellata). Chiave privata: file o `keychain:<servizio>` (`backup_identity`).

Rotazione delle copie cifrate: 14 giornaliere, 8 settimanali, 12 mensili.

## Installazione (Ubuntu 24.04, x86_64 o arm64)
```bash
sudo deploy/install.sh --config askesis.toml --timezone Area/City
```
Idempotente: rieseguirlo aggiorna codice (`--ref`), peer (`--peers`) e chiavi (`--agent-key`, `--pull-key`).
Senza `--peers` l'SSH resta raggiungibile da internet (solo chiavi) per completare la prima configurazione; appena i
peer sono impostati non resta nulla di raggiungibile da fuori.

**Ricreare tutto altrove:** nuova macchina → decifrare l'ultimo backup sul proprio computer
(`age -d -i chiave.txt file.db.gz.age | gunzip > askesis.db`) → copiare `askesis.db` e la configurazione privata →
`sudo deploy/install.sh --config askesis.toml --db askesis.db`. Password della dashboard e token dei dispositivi non
sono nei backup: si ricreano (`bin/ak web set-password`, pagina Sincronizzazione).

## Servizi
| unità | cosa fa |
|---|---|
| `askesis-dashboard.service` | dashboard; riparte da sola se si ferma o se cambia l'indirizzo della VPN |
| `askesis-backup.timer` | backup, test di ripristino, copia cifrata, copia esterna |
| `askesis-web-check.timer` | controllo di raggiungibilità ogni 10 minuti; avvisi in `/var/lib/askesis/alerts.log` |

Apple Notes (scheda della palestra, nota del piano) funziona solo su macOS: con il server si usa la pagina Palestra
della dashboard.
