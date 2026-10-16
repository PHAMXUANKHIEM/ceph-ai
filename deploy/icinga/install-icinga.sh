#!/usr/bin/env bash
# Install and configure the Icinga stack Ceph AI hands alerting to (plan icinga-alerting-handoff, I1).
#
#   sudo bash install-icinga.sh            # on a fresh Ubuntu 24.04 host; re-runnable
#
# Installs from the official repository (packages.icinga.com): Icinga 2 with its API, IcingaDB
# with its own Redis, MariaDB, Icinga Web 2 with IcingaDB Web on Apache. Every password is
# generated here, kept in 0600 files under /root/.config/icinga-setup and never printed:
#   web-admin.pass   Icinga Web 2 user "admin"
#   api-ceph-ai.pass API user "ceph-ai" (comments and passive check results only; given to Ceph AI)
# Ceph checks and the Ceph AI / Telegram notifications are configured by configure-ceph-ai.sh.
set -euo pipefail

SECRETS=/root/.config/icinga-setup
say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
secret_file() {  # secret_file <name>: create once, print its path
  local path="$SECRETS/$1"
  [ -s "$path" ] || (umask 077; openssl rand -hex 20 > "$path")
  printf '%s' "$path"
}

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || { echo "tested on Ubuntu 24.04 only (this is $PRETTY_NAME)" >&2; exit 1; }
install -d -m 0700 "$SECRETS"
export DEBIAN_FRONTEND=noninteractive

say "Icinga repository"
if [ ! -f /etc/apt/sources.list.d/icinga.list ]; then
  apt-get update -qq
  apt-get install -y -qq apt-transport-https wget gnupg ca-certificates >/dev/null
  wget -qO /tmp/icinga-archive-keyring.deb "https://packages.icinga.com/icinga-archive-keyring_latest+ubuntu${VERSION_ID}.deb"
  apt-get install -y -qq /tmp/icinga-archive-keyring.deb >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/icinga-archive-keyring.gpg] https://packages.icinga.com/ubuntu icinga-${VERSION_CODENAME} main" \
    > /etc/apt/sources.list.d/icinga.list
fi
apt-get update -qq

say "packages"
apt-get install -y -qq icinga2 monitoring-plugins icingadb icingadb-redis mariadb-server \
  icingaweb2 icingadb-web icingacli libapache2-mod-php php-mysql php-curl php-intl php-gd php-xml \
  python3 jq curl ceph-common >/dev/null

say "MariaDB databases"
systemctl enable --now mariadb >/dev/null
DB_ICINGADB="$(cat "$(secret_file db-icingadb.pass)")"
DB_WEB="$(cat "$(secret_file db-icingaweb2.pass)")"
mysql --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS icingadb;
CREATE DATABASE IF NOT EXISTS icingaweb2;
CREATE USER IF NOT EXISTS 'icingadb'@'localhost' IDENTIFIED BY '${DB_ICINGADB}';
ALTER USER 'icingadb'@'localhost' IDENTIFIED BY '${DB_ICINGADB}';
GRANT ALL ON icingadb.* TO 'icingadb'@'localhost';
CREATE USER IF NOT EXISTS 'icingaweb2'@'localhost' IDENTIFIED BY '${DB_WEB}';
ALTER USER 'icingaweb2'@'localhost' IDENTIFIED BY '${DB_WEB}';
GRANT ALL ON icingaweb2.* TO 'icingaweb2'@'localhost';
GRANT SELECT ON icingadb.* TO 'icingaweb2'@'localhost';
SQL
if [ "$(mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='icingadb'")" = 0 ]; then
  mysql icingadb < /usr/share/icingadb/schema/mysql/schema.sql
fi
if [ "$(mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='icingaweb2'")" = 0 ]; then
  mysql icingaweb2 < /usr/share/icingaweb2/schema/mysql.schema.sql
fi

say "Icinga 2 API and IcingaDB feature"
[ -f /var/lib/icinga2/certs/"$(hostname -f)".crt ] || icinga2 api setup >/dev/null
icinga2 feature enable icingadb >/dev/null
API_WEB="$(cat "$(secret_file api-icingaweb2.pass)")"
API_CEPH_AI="$(cat "$(secret_file api-ceph-ai.pass)")"
cat > /etc/icinga2/conf.d/api-users.conf <<CONF
// Managed by deploy/icinga/install-icinga.sh. Passwords live in ${SECRETS}.
object ApiUser "root" {
  password = "$(cat "$(secret_file api-root.pass)")"
  permissions = [ "*" ]
}
object ApiUser "icingaweb2" {
  password = "${API_WEB}"
  permissions = [ "actions/*", "objects/modify/*", "objects/query/*", "status/query" ]
}
// Ceph AI writes its diagnosis back as comments and reports its own heartbeat/forecast
// as passive check results; nothing else.
object ApiUser "ceph-ai" {
  password = "${API_CEPH_AI}"
  permissions = [ "actions/add-comment", "actions/process-check-result", "objects/query/Host", "objects/query/Service" ]
}
CONF
chmod 0640 /etc/icinga2/conf.d/api-users.conf; chown root:nagios /etc/icinga2/conf.d/api-users.conf

say "IcingaDB"
systemctl enable --now icingadb-redis >/dev/null
cat > /etc/icingadb/config.yml <<YAML
database:
  type: mysql
  host: localhost
  port: 3306
  database: icingadb
  user: icingadb
  password: ${DB_ICINGADB}
redis:
  host: localhost
  port: 6380
logging:
  level: info
YAML
chmod 0640 /etc/icingadb/config.yml; chown root:icingadb /etc/icingadb/config.yml
icinga2 daemon -C >/dev/null
systemctl enable --now icinga2 >/dev/null; systemctl restart icinga2
systemctl enable --now icingadb >/dev/null; systemctl restart icingadb

say "Icinga Web 2"
install -d -m 2770 -o www-data -g icingaweb2 /etc/icingaweb2 /etc/icingaweb2/enabledModules \
  /etc/icingaweb2/modules /etc/icingaweb2/modules/icingadb
cat > /etc/icingaweb2/resources.ini <<INI
[icingaweb_db]
type = "db"
db = "mysql"
host = "localhost"
dbname = "icingaweb2"
username = "icingaweb2"
password = "${DB_WEB}"
charset = "utf8mb4"

[icingadb]
type = "db"
db = "mysql"
host = "localhost"
dbname = "icingadb"
username = "icingaweb2"
password = "${DB_WEB}"
charset = "utf8mb4"
INI
cat > /etc/icingaweb2/config.ini <<'INI'
[global]
show_stacktraces = "0"
config_resource = "icingaweb_db"

[logging]
log = "syslog"
level = "ERROR"
application = "icingaweb2"
INI
cat > /etc/icingaweb2/authentication.ini <<'INI'
[icingaweb2]
backend = "db"
resource = "icingaweb_db"
INI
cat > /etc/icingaweb2/groups.ini <<'INI'
[icingaweb2]
backend = "db"
resource = "icingaweb_db"
INI
cat > /etc/icingaweb2/roles.ini <<'INI'
[Administrators]
users = "admin"
permissions = "*"
INI
cat > /etc/icingaweb2/modules/icingadb/config.ini <<'INI'
[icingadb]
resource = "icingadb"
INI
cat > /etc/icingaweb2/modules/icingadb/redis.ini <<'INI'
[redis1]
host = "localhost"
port = "6380"
INI
cat > /etc/icingaweb2/modules/icingadb/commandtransports.ini <<INI
[icinga2]
transport = "api"
host = "localhost"
port = "5665"
username = "icingaweb2"
password = "${API_WEB}"
INI
icingacli module enable icingadb >/dev/null 2>&1 || true
chown -R www-data:icingaweb2 /etc/icingaweb2; chmod -R g+rwX,o-rwx /etc/icingaweb2
WEB_ADMIN="$(cat "$(secret_file web-admin.pass)")"
HASH="$(printf '%s' "$WEB_ADMIN" | php -r 'echo password_hash(trim(stream_get_contents(STDIN)), PASSWORD_DEFAULT);')"
mysql icingaweb2 -e "INSERT INTO icingaweb_user (name, active, password_hash) VALUES ('admin', 1, '${HASH}')
  ON DUPLICATE KEY UPDATE active = 1, password_hash = VALUES(password_hash);"
a2enmod -q php8.3 >/dev/null 2>&1 || true
a2enconf -q icingaweb2 >/dev/null 2>&1 || true
systemctl enable --now apache2 >/dev/null; systemctl reload apache2

say "checks"
systemctl is-active --quiet icinga2 icingadb icingadb-redis mariadb apache2
curl -fsS -c /tmp/.iw -b /tmp/.iw -L -o /dev/null http://127.0.0.1/icingaweb2/authentication/login; rm -f /tmp/.iw
# The only kind of query the ceph-ai API user may make.
curl -fsS -k -u "ceph-ai:${API_CEPH_AI}" https://127.0.0.1:5665/v1/objects/services >/dev/null
say "OK: http://$(hostname -I | awk '{print $1}')/icingaweb2  (user admin, password in ${SECRETS}/web-admin.pass)"
