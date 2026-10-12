#!/usr/bin/env bash
# Askesis server: complete, repeatable installation on Ubuntu 24.04 (x86_64 or arm64). Idempotent: run it again to
# update the code, change peers or keys. Recreating everything elsewhere = new machine + this script + latest backup.
#
#   sudo deploy/install.sh --config askesis.toml [--db askesis.db] [--peers 100.x.y.z,100.x.y.w]
#                          [--agent-key "ssh-ed25519 …"] [--pull-key "ssh-ed25519 …"] [--with-nordvpn]
#                          [--ref <commit|tag>] [--timezone Area/City]
#
# --config   private configuration (never in the repository); installed as /etc/askesis/askesis.toml
# --db       plain database restored from a sealed backup (decrypted on the athlete's own machine); refused if a
#            database already exists, unless --replace-db
# --peers    Meshnet IPv4 addresses allowed to reach SSH and the dashboard. Without peers SSH stays reachable from
#            anywhere (keys only) so the first setup can be completed; with peers, nothing is reachable from the
#            internet any more.
# --agent-key  public key allowed to run only `ak …` (forced command, see deploy/ak-gate)
# --pull-key   public key allowed only to read sealed backups (rrsync, read-only)
set -euo pipefail

REPO_URL="https://github.com/fedefranchini/askesis.git"
REF=""
CONFIG=""
DB=""
REPLACE_DB=0
PEERS=""
AGENT_KEY=""
PULL_KEY=""
WITH_NORDVPN=0
TZ_NAME=""
UV_VERSION="0.12.6"

APP=/opt/askesis
TOOLS=/opt/askesis-tools
DATA=/var/lib/askesis
ETC=/etc/askesis

die() { echo "✗ $*" >&2; exit 1; }
step() { echo "— $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --replace-db) REPLACE_DB=1; shift ;;
    --peers) PEERS="$2"; shift 2 ;;
    --agent-key) AGENT_KEY="$2"; shift 2 ;;
    --pull-key) PULL_KEY="$2"; shift 2 ;;
    --with-nordvpn) WITH_NORDVPN=1; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --timezone) TZ_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "opzione sconosciuta: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "eseguire con sudo"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "previsto Ubuntu (trovato ${ID:-?})"
[[ -n "$CONFIG" || -f $ETC/askesis.toml ]] || die "manca --config (prima installazione)"
[[ -z "$CONFIG" || -f "$CONFIG" ]] || die "file di configurazione non trovato: $CONFIG"
[[ -z "$DB" || -f "$DB" ]] || die "database non trovato: $DB"
for ip in ${PEERS//,/ }; do
  [[ "$ip" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}$ ]] \
    || die "peer non valido (atteso un IPv4 Meshnet 100.64.0.0/10): $ip"
done
START=$(date +%s)

step "pacchetti di sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-venv age rclone rsync nftables unattended-upgrades sqlite3 >/dev/null

step "aggiornamenti di sicurezza automatici (riavvio alle 04:30 se necessario)"
cat > /etc/apt/apt.conf.d/52askesis-unattended <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

if [[ -n "$TZ_NAME" ]]; then
  step "fuso orario $TZ_NAME"
  timedatectl set-timezone "$TZ_NAME"
fi

mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [[ $mem_kb -lt 2000000 && ! -f /swapfile ]]; then
  step "file di swap da 1 GB (memoria sotto i 2 GB)"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

step "utente di servizio e cartelle"
id askesis >/dev/null 2>&1 || useradd --system --home-dir $DATA --shell /bin/bash askesis
install -d -m 0700 -o askesis -g askesis $DATA $DATA/reports
install -d -m 0750 -o root -g askesis $ETC

step "uv $UV_VERSION (versione fissata)"
[[ -x $TOOLS/bin/python ]] || python3 -m venv $TOOLS
$TOOLS/bin/pip install -q "uv==$UV_VERSION"

step "codice da $REPO_URL ${REF:+(ref $REF)}"
if [[ -d $APP/.git ]]; then
  git -C $APP fetch -q --tags origin
else
  git clone -q "$REPO_URL" $APP
fi
git -C $APP checkout -q --detach "${REF:-origin/main}"
chown -R root:root $APP
cd $APP
UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/var/cache/askesis-uv $TOOLS/bin/uv sync -q --frozen --no-dev --extra dashboard
chmod -R a+rX $APP

step "configurazione"
[[ -z "$CONFIG" ]] || install -m 0640 -o root -g askesis "$CONFIG" $ETC/askesis.toml
cat > $ETC/env <<EOF
ASKESIS_CONFIG=$ETC/askesis.toml
ASKESIS_DB_PATH=$DATA/askesis.db
ASKESIS_BACKUP_DIR=$DATA/backups
ASKESIS_STAGING_DIR=$DATA/staging
ASKESIS_BACKUP_SEALED_DIR=$DATA/sealed
ASKESIS_REPORTS_DIR=$DATA/reports
ASKESIS_ALERTS_FILE=$DATA/alerts.log
RCLONE_CONFIG=$ETC/rclone.conf
UV_CACHE_DIR=$DATA/.cache/uv
UV_PYTHON_DOWNLOADS=never
UV_NO_SYNC=1
PATH=$TOOLS/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
chmod 0640 $ETC/env && chgrp askesis $ETC/env
[[ -f $ETC/rclone.conf ]] || install -m 0640 -o root -g askesis /dev/null $ETC/rclone.conf

if [[ -n "$DB" ]]; then
  if [[ -f $DATA/askesis.db && $REPLACE_DB -eq 0 ]]; then
    die "esiste già un database in $DATA: usare --replace-db per sostituirlo (il vecchio viene conservato)"
  fi
  [[ ! -f $DATA/askesis.db ]] || mv $DATA/askesis.db "$DATA/askesis.db.before-$(date +%Y%m%d-%H%M%S)"
  rm -f $DATA/askesis.db-wal $DATA/askesis.db-shm
  step "ripristino del database"
  [[ "$(sqlite3 "$DB" 'PRAGMA integrity_check')" == "ok" ]] || die "il database da ripristinare non è integro"
  install -m 0600 -o askesis -g askesis "$DB" $DATA/askesis.db
fi
systemd-run --quiet --wait --pipe --uid=askesis --gid=askesis --property=EnvironmentFile=$ETC/env $APP/bin/ak init

step "accesso SSH: solo chiavi"
install -m 0644 $APP/deploy/sshd-askesis.conf /etc/ssh/sshd_config.d/10-askesis.conf
install -m 0755 $APP/deploy/ak-gate /usr/local/sbin/askesis-ak-gate
install -d -m 0700 -o askesis -g askesis $DATA/.ssh
keys=$DATA/.ssh/authorized_keys
touch $keys
if [[ -n "$AGENT_KEY" ]]; then
  grep -vF ' askesis-agent' $keys > $keys.new || true
  echo "restrict,command=\"/usr/local/sbin/askesis-ak-gate\" ${AGENT_KEY%% askesis-*} askesis-agent" >> $keys.new
  mv $keys.new $keys
fi
if [[ -n "$PULL_KEY" ]]; then
  grep -vF ' askesis-pull' $keys > $keys.new || true
  echo "restrict,command=\"/usr/bin/rrsync -ro $DATA/sealed\" ${PULL_KEY%% askesis-*} askesis-pull" >> $keys.new
  mv $keys.new $keys
fi
chown askesis:askesis $keys && chmod 0600 $keys
sshd -t
systemctl reload ssh

if [[ $WITH_NORDVPN -eq 1 ]] && ! command -v nordvpn >/dev/null; then
  step "NordVPN (installer ufficiale scaricato in un file, non eseguito dalla rete)"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/install.sh" https://downloads.nordcdn.com/apps/linux/install.sh
  echo "  sha256 $(sha256sum "$tmp/install.sh" | cut -d' ' -f1)"
  # used only to add the vendor's signed repository: its own apt prompt aborts without a terminal
  sh "$tmp/install.sh" </dev/null || true
  rm -rf "$tmp"
  apt-get install -y -q --no-install-recommends nordvpn >/dev/null  # daemon and CLI only, no desktop app
  command -v nordvpn >/dev/null || die "installazione di NordVPN non riuscita"
  usermod -aG nordvpn askesis || true
fi

step "firewall"
[[ -z "$PEERS" ]] || echo "${PEERS//,/$'\n'}" > $ETC/peers
touch $ETC/peers
elements=$(paste -sd, $ETC/peers)
peers_line=""
[[ -z "$elements" ]] || peers_line="elements = { $elements }"
rules=$(sed "s|__PEERS__|$peers_line|" $APP/deploy/nftables.conf)
if [[ -s $ETC/peers ]]; then
  rules=${rules//__BOOTSTRAP_SSH__/}
  echo "  SSH e dashboard solo da Meshnet: $(paste -sd' ' $ETC/peers)"
else
  rules=${rules//__BOOTSTRAP_SSH__/tcp dport 22 accept  # first setup only: closed as soon as peers are set}
  echo "  nessun peer: SSH raggiungibile da internet (solo chiavi) finché non si passa --peers"
fi
if systemctl is-enabled -q netfilter-persistent 2>/dev/null; then  # cloud image defaults (iptables): replaced once
  systemctl disable -q --now netfilter-persistent
  iptables -F INPUT
fi
echo "$rules" > /etc/nftables.conf
nft -c -f /etc/nftables.conf
systemctl enable -q --now nftables
nft -f /etc/nftables.conf

step "servizi"
install -m 0644 $APP/deploy/systemd/*.service $APP/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now askesis-backup.timer askesis-web-check.timer
systemctl enable -q askesis-dashboard.service
systemctl restart askesis-dashboard.service

echo "✓ installazione completata in $(( $(date +%s) - START )) s · codice $(git -C $APP rev-parse --short HEAD)"
echo "  stato: systemctl status askesis-dashboard · log: $DATA/web.log · avvisi: $DATA/alerts.log"
