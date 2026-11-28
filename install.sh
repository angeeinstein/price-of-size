#!/usr/bin/env bash
# The Price of Size: installer, updater and uninstaller for every role.
#
#   curl -fsSL https://raw.githubusercontent.com/angeeinstein/price-of-size/main/install.sh | sudo bash
#
# Roles: node (default), coordinator, database, all-in-one. Without --role an
# interactive menu asks only what the role needs. Non-interactive example:
#
#   sudo bash install.sh --role node --coordinator https://pos.example.org --enroll ABCD-EFGH-JKLM-NPQR
#
# It installs a pinned release (GitHub release `v<version>` and its release.json,
# which lists every image by digest), never whatever is on `main`. Running it
# again repairs an installation instead of breaking it.
#
# Run without options on a machine where it is installed, it offers a menu:
# update, repair, add a node here (coordinator), uninstall (keeping or deleting data).
#
#   --update        repair the installed role and, on the coordinator, update everything
#                   to the latest release: the same as the panel's Update button
#                   (coordinator, then one node, then the others), with its progress
#   --uninstall     remove containers and services, keep data (add --purge to delete data)
#
# See docs/operations.md for details.

set -Eeuo pipefail
umask 077

REPO="${POS_REPO:-angeeinstein/price-of-size}"
ETC=/etc/pos
CONF="$ETC/install.conf"
NET=pos-net
LOCAL_COORDINATOR=http://pos-caddy:80  # Caddy as seen from inside the coordinator network
DOCKER_LOG_OPTS=(--log-opt max-size=10m --log-opt max-file=3)

# --- options -----------------------------------------------------------------------

ROLE="" VERSION="" MODE="" COORDINATOR_URL="" ENROLL_CODE="" NODE_NAME=""
DB_URL="" RAW_STORE="" BACKUP_DIR="" HTTP_PORT="" PG_ALLOW="" PG_DATA_DIR=""
LISTEN="" TRUSTED_PROXY="" LOCAL_NODE=""
CF_ID="" CF_SECRET="" REG_USER="" REG_TOKEN="" REG_SERVER="" RELEASE_FILE=""
REGISTRY_ENV=()
ACTION=install NONINTERACTIVE=0 PURGE=0 ASSUME_YES=0

usage() {
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --role ROLE            node | coordinator | database | all-in-one
  --version X.Y.Z        release to install (default: latest)
  --mode agent|simple    node only. agent (default): updates steered by the coordinator.
                         simple: a single worker container, updated with --update by hand.
  --coordinator URL      node: coordinator URL, or "local" if the coordinator runs on this machine
  --enroll CODE          node: one-time enrollment code from the control panel
  --name NAME            node: display name (default: hostname)
  --cf-client-id ID      Cloudflare Access service token (optional)
  --cf-client-secret S
  --db-url URL           coordinator: postgresql+psycopg://user:pass@host:5432/pos
  --raw-store PATH       coordinator: raw store directory (NAS mount), default /srv/pos/raw
  --backup-dir PATH      coordinator/database: backup directory, default /srv/pos/backups
  --http-port N          coordinator: HTTP port for Caddy (default 8080)
  --listen ADDR          coordinator: address Caddy listens on: 127.0.0.1 (default, this
                         machine only) or 0.0.0.0 / a LAN address (cloudflared in another
                         LXC, the control panel from the LAN)
  --trusted-proxy IP[,..]  coordinator: the cloudflared host(s) whose visitor address
                         (CF-Connecting-IP) is believed (IPs or CIDRs)
  --pg-allow CIDR[,..]   database: networks allowed to connect (default: private ranges)
  --pg-data-dir PATH     database: data directory (first install only)
  --registry-user U      only for private images (GHCR user + token with read:packages)
  --registry-token T      (kept in /etc/pos/registry.env: re-runs need not repeat them)
  --registry-server H    registry of the private images (default ghcr.io)
  --release-file PATH    use a local release.json instead of downloading it (testing)
  --non-interactive      never prompt; fail if something required is missing
  --yes                  answer yes to confirmations (e.g. --uninstall --purge)
  --update | --uninstall | --purge | -h | --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift ;;
    --version) VERSION="${2#v}"; shift ;;
    --mode) MODE="$2"; shift ;;
    --coordinator) COORDINATOR_URL="$2"; shift ;;
    --enroll) ENROLL_CODE="$2"; shift ;;
    --name) NODE_NAME="$2"; shift ;;
    --cf-client-id) CF_ID="$2"; shift ;;
    --cf-client-secret) CF_SECRET="$2"; shift ;;
    --db-url) DB_URL="$2"; shift ;;
    --raw-store) RAW_STORE="$2"; shift ;;
    --backup-dir) BACKUP_DIR="$2"; shift ;;
    --http-port) HTTP_PORT="$2"; shift ;;
    --listen) LISTEN="$2"; shift ;;
    --trusted-proxy) TRUSTED_PROXY="$2"; shift ;;
    --pg-allow) PG_ALLOW="$2"; shift ;;
    --pg-data-dir) PG_DATA_DIR="$2"; shift ;;
    --registry-user) REG_USER="$2"; shift ;;
    --registry-token) REG_TOKEN="$2"; shift ;;
    --registry-server) REG_SERVER="$2"; shift ;;
    --release-file) RELEASE_FILE="$2"; shift ;;
    --non-interactive) NONINTERACTIVE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --update) ACTION=update ;;
    --uninstall) ACTION=uninstall ;;
    --purge) PURGE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# --- helpers -----------------------------------------------------------------------

if [[ -t 1 ]]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'; else B="" G="" Y="" R="" N=""; fi
info() { echo "${B}==>${N} $*"; }
ok() { echo "${G}ok${N}  $*"; }
warn() { echo "${Y}warning:${N} $*" >&2; }
die() { echo "${R}error:${N} $*" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# Prompts read from the terminal, so `curl ... | sudo bash` still works.
can_prompt() { [[ $NONINTERACTIVE -eq 0 ]] && { : </dev/tty; } 2>/dev/null; }

ask() {  # ask VAR "question" "default"
  local __var=$1 q=$2 def=${3:-} ans
  if [[ -n "${!__var}" ]]; then return; fi
  if ! can_prompt; then
    [[ -n "$def" ]] || die "missing required option for: $q"
    printf -v "$__var" '%s' "$def"; return
  fi
  read -r -p "$q${def:+ [$def]}: " ans </dev/tty || true
  printf -v "$__var" '%s' "${ans:-$def}"
  [[ -n "${!__var}" ]] || die "$q is required"
}

ask_secret() {
  local __var=$1 q=$2 ans
  if [[ -n "${!__var}" ]] || ! can_prompt; then return; fi
  read -r -s -p "$q (leave empty to skip): " ans </dev/tty || true
  echo
  printf -v "$__var" '%s' "$ans"
}

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  can_prompt || return 1
  local ans
  read -r -p "$1 [y/N]: " ans </dev/tty || true
  [[ "$ans" =~ ^[Yy] ]]
}

is_loopback() { [[ -z "$1" || "$1" == 127.* || "$1" == ::1 || "$1" == localhost ]]; }

# The coordinator as seen from this machine (wherever Caddy listens).
local_url() {
  local host=${LISTEN:-127.0.0.1}
  # a wildcard: the loopback of its family (Docker publishes "::" on IPv6 only)
  case "$host" in 0.0.0.0) host=127.0.0.1 ;; ::) host=::1 ;; esac
  [[ "$host" == *:* ]] && host="[$host]"
  echo "http://$host:$HTTP_PORT"
}

random_secret() { head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | head -c 40; }

need_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo)"; }

save_conf() {  # persist answers for --update/--repair; secrets live in the env files
  install -d -m 0700 "$ETC"
  cat >"$CONF" <<EOF
ROLE=$ROLE
MODE=$MODE
VERSION=$VERSION
RAW_STORE=$RAW_STORE
BACKUP_DIR=$BACKUP_DIR
HTTP_PORT=$HTTP_PORT
LISTEN=$LISTEN
TRUSTED_PROXY=$TRUSTED_PROXY
LOCAL_NODE=$LOCAL_NODE
PG_ALLOW=$PG_ALLOW
EOF
  chmod 0600 "$CONF"
}

load_conf() {
  [[ -f "$CONF" ]] || return 0
  local k v
  while IFS='=' read -r k v; do
    case "$k" in
      ROLE) [[ -n "$ROLE" ]] || ROLE=$v ;;
      MODE) [[ -n "$MODE" ]] || MODE=$v ;;
      INSTALLED_VERSION|VERSION) INSTALLED_VERSION=$v ;;
      RAW_STORE) [[ -n "$RAW_STORE" ]] || RAW_STORE=$v ;;
      BACKUP_DIR) [[ -n "$BACKUP_DIR" ]] || BACKUP_DIR=$v ;;
      HTTP_PORT) [[ -n "$HTTP_PORT" ]] || HTTP_PORT=$v ;;
      LISTEN) [[ -n "$LISTEN" ]] || LISTEN=$v ;;
      TRUSTED_PROXY) [[ -n "$TRUSTED_PROXY" ]] || TRUSTED_PROXY=$v ;;
      LOCAL_NODE) [[ -n "$LOCAL_NODE" ]] || LOCAL_NODE=$v ;;
      PG_ALLOW) [[ -n "$PG_ALLOW" ]] || PG_ALLOW=$v ;;
    esac
  done <"$CONF"
}
INSTALLED_VERSION=""  # set by load_conf
EXPLICIT_RELEASE=0     # --version or --release-file given: update to exactly that

detect_os() {
  local os_release=${POS_OS_RELEASE:-/etc/os-release}
  [[ -r "$os_release" ]] || die "unsupported system (no $os_release)"
  # Read in a subshell: os-release defines VERSION, NAME, ID ... which must not
  # overwrite this script's own variables (VERSION is the release to install).
  local vars
  vars=$(
    # shellcheck disable=SC1090
    . "$os_release"
    # one line, fields separated by US (0x1f): empty fields (no ID_LIKE or
    # VERSION_CODENAME) survive, where trailing newlines would be stripped
    printf '%s\x1f%s\x1f%s\x1f' "${ID:-}" "${ID_LIKE:-}" "${VERSION_CODENAME:-}"
  )
  IFS=$'\x1f' read -r OS_ID OS_LIKE OS_CODENAME _ <<<"$vars"
  case "$OS_ID $OS_LIKE" in
    *debian*|*ubuntu*) ;;
    *) die "only Debian, Ubuntu and derivatives (incl. Raspberry Pi OS) are supported, found $OS_ID" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "unsupported CPU architecture $(uname -m) (need amd64 or arm64)" ;;
  esac
}

# Run a command that takes a while, showing what it does and for how long (on a
# terminal: one line with the elapsed time, replaced when it ends; otherwise one
# line at the start). Its output is kept and shown only if it fails.
progress() {  # progress "what it does" command...
  local what=$1; shift
  local out; out=$(mktemp)
  if [[ ! -t 2 ]]; then
    echo "    $what ..." >&2
    local rc=0
    "$@" >"$out" 2>&1 || rc=$?
    ((rc == 0)) || tail -n 20 "$out" >&2
    rm -f "$out"
    return "$rc"
  fi
  # in a subshell without the ERR trap: a failure is reported here, once
  ( trap - ERR; set +e; "$@" ) >"$out" 2>&1 &
  local pid=$! start=$SECONDS rc=0
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r    %s ... %ds ' "$what" $((SECONDS - start)) >&2
    sleep 1
  done
  wait "$pid" || rc=$?
  printf '\r\033[K' >&2
  if ((rc != 0)); then tail -n 20 "$out" >&2; fi
  rm -f "$out"
  return "$rc"
}

apt_update() { progress "updating the package lists" apt-get update -qq; }

apt_install() {
  progress "installing $*" env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends "$@"
}

ensure_base_tools() {
  local missing=()
  for t in curl jq ca-certificates gpg; do
    case $t in
      ca-certificates) [[ -e /etc/ssl/certs/ca-certificates.crt ]] || missing+=(ca-certificates) ;;
      gpg) command -v gpg >/dev/null || missing+=(gnupg) ;;
      *) command -v "$t" >/dev/null || missing+=("$t") ;;
    esac
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    info "installing ${missing[*]}"
    apt_update
    apt_install "${missing[@]}"
  fi
}

ensure_docker() {
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    ok "docker $(docker version --format '{{.Server.Version}}')"
    return
  fi
  if ! command -v docker >/dev/null; then
    info "installing Docker from download.docker.com"
    local distro=$OS_ID
    [[ "$distro" == raspbian ]] && distro=debian
    [[ "$distro" != ubuntu && "$distro" != debian ]] && {
      [[ "$OS_LIKE" == *ubuntu* ]] && distro=ubuntu || distro=debian
    }
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$distro/gpg" | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$distro $OS_CODENAME stable" \
      >/etc/apt/sources.list.d/docker.list
    apt_update
    apt_install docker-ce docker-ce-cli containerd.io
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "docker is installed but not running (in an LXC, enable nesting)"
  ok "docker $(docker version --format '{{.Server.Version}}')"
}

# Registry credentials (private images) are kept in a root-only file, so a re-run or
# --update without --registry-user/--registry-token keeps them: the agent pulls
# signed updates with them, not with the host's docker login.
load_registry() {
  local file=$ETC/registry.env
  if [[ -n "$REG_USER" || -n "$REG_TOKEN" ]]; then
    [[ -n "$REG_USER" && -n "$REG_TOKEN" ]] || die "--registry-user and --registry-token go together"
    REG_SERVER=${REG_SERVER:-ghcr.io}
    write_env "$file" "POS_AGENT_REGISTRY_USERNAME=$REG_USER" "POS_AGENT_REGISTRY_PASSWORD=$REG_TOKEN" \
      "POS_AGENT_REGISTRY_SERVER=$REG_SERVER"
  elif [[ -f "$file" ]]; then
    REG_USER=$(env_get "$file" POS_AGENT_REGISTRY_USERNAME)
    REG_TOKEN=$(env_get "$file" POS_AGENT_REGISTRY_PASSWORD)
    REG_SERVER=$(env_get "$file" POS_AGENT_REGISTRY_SERVER)
  fi
  REG_SERVER=${REG_SERVER:-ghcr.io}
}

# Passed to the agents, which pull signed updates themselves.
registry_env() {
  REGISTRY_ENV=()
  [[ -n "$REG_USER" ]] || return 0
  REGISTRY_ENV=(-e "POS_AGENT_REGISTRY_USERNAME=$REG_USER" -e "POS_AGENT_REGISTRY_PASSWORD=$REG_TOKEN"
    -e "POS_AGENT_REGISTRY_SERVER=$REG_SERVER")
}

docker_login() {
  load_registry
  if [[ -n "$REG_USER" && -n "$REG_TOKEN" ]]; then
    echo "$REG_TOKEN" | docker login "$REG_SERVER" -u "$REG_USER" --password-stdin >/dev/null
    ok "logged in to $REG_SERVER as $REG_USER"
  fi
}

# --- releases ------------------------------------------------------------------------

resolve_release() {
  install -d -m 0700 "$ETC"
  if [[ -n "$RELEASE_FILE" ]]; then
    cp "$RELEASE_FILE" "$ETC/release.json.new"
  else
    if [[ -z "$VERSION" ]]; then
      VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | jq -r .tag_name)
      VERSION=${VERSION#v}
      [[ -n "$VERSION" && "$VERSION" != null ]] || die "could not determine the latest release"
    fi
    curl -fsSL "https://github.com/$REPO/releases/download/v$VERSION/release.json" -o "$ETC/release.json.new" \
      || die "release v$VERSION not found"
  fi
  # Every image must be pinned by digest.
  jq -e '.version and (.images | to_entries | all(.value | test("@sha256:[0-9a-f]{64}$")))' \
    "$ETC/release.json.new" >/dev/null || die "release.json is malformed or not digest-pinned"
  VERSION=$(jq -r .version "$ETC/release.json.new")
  chmod 0644 "$ETC/release.json.new"
  mv "$ETC/release.json.new" "$ETC/release.json"
  ok "release $VERSION"
}

image() { jq -r --arg c "$1" '.images[$c] // empty' "$ETC/release.json"; }

# Has the worker reported a successful heartbeat since a given time (unix seconds)?
worker_healthy_since() {
  local tool; tool=$(image agent)
  docker run --rm --entrypoint python -v pos-worker-exchange:/v:ro "$tool" -c \
    "import json,sys; sys.exit(0 if json.load(open('/v/health.json'))['ok_at'] >= $1 else 1)" \
    >/dev/null 2>&1
}

# Does a file exist in a Docker volume? Uses the (pinned) agent image as a tool box.
vol_has() {  # vol_has VOLUME FILE
  docker volume inspect "$1" >/dev/null 2>&1 || return 1
  local tool; tool=$(image agent)
  docker image inspect "$tool" >/dev/null 2>&1 || pull "$tool"
  docker run --rm --entrypoint test -v "$1:/v:ro" "$tool" -f "/v/$2" >/dev/null 2>&1
}

pull() {
  local ref=$1
  [[ -n "$ref" ]] || die "release has no image for this component"
  progress "downloading ${ref%%@*}" docker pull -q "$ref" || die "could not pull $ref"
}

# Run (or keep) a long-lived container. It is recreated only if its image or its
# configuration changed, so running the installer again does not disturb a healthy
# installation. The configuration is fingerprinted in a label.
run_container() {  # run_container NAME IMAGE [docker run args...] -- [cmd...]
  local name=$1 img=$2; shift 2
  local args=() cmd=() seen=0
  for a in "$@"; do
    if [[ $seen -eq 0 && "$a" == "--" ]]; then seen=1; continue; fi
    if [[ $seen -eq 0 ]]; then args+=("$a"); else cmd+=("$a"); fi
  done
  # Image and configuration are compared separately: an agent that updated itself
  # keeps the labels of its predecessor, and must still count as up to date.
  local fp; fp=$(printf '%s\0' "${args[@]}" "${cmd[@]}" | sha256sum | cut -c1-16)
  local current_fp current_img running
  current_fp=$(docker inspect -f '{{index .Config.Labels "org.price-of-size.config"}}' "$name" 2>/dev/null || true)
  current_img=$(docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)
  running=$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)
  if [[ "$current_fp" == "$fp" && "$current_img" == "$img" ]]; then
    [[ "$running" == true ]] || docker start "$name" >/dev/null
    ok "$name is up to date"
    return
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --label "org.price-of-size.config=$fp" "${DOCKER_LOG_OPTS[@]}" \
    "${args[@]}" "$img" "${cmd[@]}" >/dev/null
  ok "started $name"
}

# The image an agent container should run: the one it already runs, if any.
# Agents update themselves to coordinator-signed releases; the installer must
# never downgrade them to the (older) release it was started with.
agent_image() {  # agent_image NAME
  local current
  current=$(docker inspect -f '{{.Config.Image}}' "$1" 2>/dev/null || true)
  if [[ -n "$current" ]]; then echo "$current"; else image agent; fi
}

# Configuration changes (env files, paths) reach the agent-managed containers by
# themselves: the agent fingerprints each container's full definition and recreates
# it when the definition changes.

write_env() {  # write_env FILE KEY=VALUE...   (an unchanged file is left untouched)
  local file=$1; shift
  install -d -m 0700 "$ETC"
  : >"$file.new"
  chmod 0600 "$file.new"
  local kv
  for kv in "$@"; do
    if [[ "$kv" == *=?* ]]; then printf '%s\n' "$kv" >>"$file.new"; fi
  done
  if [[ -f "$file" ]] && cmp -s "$file" "$file.new"; then
    rm -f "$file.new"
  else
    mv "$file.new" "$file"
  fi
}

# The last assignment wins, as for docker --env-file and systemd (a line appended by
# hand overrides an earlier one).
env_get() { [[ -f "$1" ]] && sed -n "s/^$2=//p" "$1" | tail -n 1 || true; }

# --- node -------------------------------------------------------------------------------

install_node() {
  local env_file=$ETC/worker.env
  [[ -n "$MODE" ]] || MODE=agent
  [[ "$MODE" == agent || "$MODE" == simple ]] || die "--mode must be agent or simple"
  COORDINATOR_URL=${COORDINATOR_URL:-$(env_get "$env_file" POS_WORKER_COORDINATOR_URL)}
  ask COORDINATOR_URL "Coordinator URL (e.g. https://pos.example.org)"
  # Kept once written, so a re-run with the same options changes nothing (the
  # code is single-use; after enrollment it is inert).
  ENROLL_CODE=${ENROLL_CODE:-$(env_get "$env_file" POS_WORKER_ENROLLMENT_CODE)}
  if ! vol_has pos-worker-state identity.json; then
    ask ENROLL_CODE "Enrollment code (control panel > Nodes > Add node)"
  fi
  NODE_NAME=${NODE_NAME:-$(env_get "$env_file" POS_WORKER_NAME)}
  ask NODE_NAME "Node name" "$(hostname)"
  CF_ID=${CF_ID:-$(env_get "$env_file" POS_WORKER_CF_ACCESS_CLIENT_ID)}
  CF_SECRET=${CF_SECRET:-$(env_get "$env_file" POS_WORKER_CF_ACCESS_CLIENT_SECRET)}
  if [[ -z "$CF_ID" ]] && can_prompt; then
    ask_secret CF_ID "Cloudflare Access client id"
    [[ -n "$CF_ID" ]] && ask_secret CF_SECRET "Cloudflare Access client secret"
  fi
  # "local": the coordinator runs on this machine. The worker then joins its Docker
  # network and talks to Caddy directly (Caddy may publish on loopback only).
  local on_net=false url=$COORDINATOR_URL
  if [[ "$ROLE" == all-in-one || "$url" == local || "$url" == "$LOCAL_COORDINATOR" ]]; then
    docker network inspect "$NET" >/dev/null 2>&1 \
      || die "--coordinator local needs a coordinator on this machine (no $NET network)"
    on_net=true
    url=$LOCAL_COORDINATOR
  fi
  # This machine's CPUs and RAM: in an LXC the worker's container would see the
  # whole host's. Re-running the installer after a resize updates them.
  local cpus mem_mb
  cpus=$(nproc 2>/dev/null || true)
  mem_mb=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || true)
  write_env "$env_file" \
    "POS_WORKER_CPU_LIMIT=$cpus" \
    "POS_WORKER_MEMORY_LIMIT_MB=$mem_mb" \
    "POS_WORKER_COORDINATOR_URL=$url" \
    "POS_WORKER_ENROLLMENT_CODE=$ENROLL_CODE" \
    "POS_WORKER_NAME=$NODE_NAME" \
    "POS_WORKER_CF_ACCESS_CLIENT_ID=$CF_ID" \
    "POS_WORKER_CF_ACCESS_CLIENT_SECRET=$CF_SECRET"
  docker volume create pos-worker-state >/dev/null
  docker volume create pos-worker-exchange >/dev/null

  if [[ "$MODE" == simple ]]; then
    # Never two workers with one identity: remove the agent and the workers it ran.
    local suffix ids
    for suffix in "" -updater -next; do docker rm -f "pos-agent-worker$suffix" >/dev/null 2>&1 || true; done
    ids=$(docker ps -aq --filter label=org.price-of-size.component=worker)
    # shellcheck disable=SC2086
    [[ -n "$ids" ]] && docker rm -f $ids >/dev/null
    local img; img=$(image worker); pull "$img"
    local net=bridge; [[ $on_net == true ]] && net=$NET
    # --env-file is read at creation only: fingerprint its content, so a changed
    # configuration (rotated token, new URL) recreates the container.
    local env_hash; env_hash=$(sha256sum "$env_file" | cut -c1-16)
    run_container pos-worker "$img" --restart unless-stopped --init --shm-size 1g \
      --network "$net" --env-file "$env_file" --label "org.price-of-size.env=$env_hash" \
      -v pos-worker-state:/var/lib/pos-worker --stop-timeout 120 --
    return
  fi
  docker rm -f pos-worker >/dev/null 2>&1 || true  # leftover from simple mode
  docker volume create pos-agent-worker-data >/dev/null
  local agent; agent=$(agent_image pos-agent-worker)
  docker image inspect "$agent" >/dev/null 2>&1 || pull "$agent"
  local started_at; started_at=$(date +%s)
  run_container pos-agent-worker "$agent" --restart always --label org.price-of-size.agent=true \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v pos-agent-worker-data:/data -v pos-worker-exchange:/exchange -v "$ETC:/etc/pos:ro" \
    -e POS_AGENT_ROLE=worker \
    -e "POS_AGENT_SELF_IMAGE=$agent" \
    -e POS_AGENT_CONTAINER_NAME=pos-agent-worker \
    -e POS_AGENT_AGENT_DATA_VOLUME=pos-agent-worker-data \
    -e POS_AGENT_EXCHANGE_VOLUME=pos-worker-exchange \
    -e POS_AGENT_STATE_VOLUME=pos-worker-state \
    -e POS_AGENT_SERVICE_ENV_FILE=/etc/pos/worker.env \
    -e "POS_AGENT_WORKER_ON_NETWORK=$on_net" \
    "${REGISTRY_ENV[@]}" --
  info "waiting for the worker to connect (up to 2 minutes)"
  for _ in $(seq 1 60); do
    if worker_healthy_since "$started_at"; then
      ok "worker is connected to the coordinator"
      return
    fi
    sleep 2
  done
  warn "the worker has not connected yet; check: docker logs pos-worker-a"
}

# --- coordinator ----------------------------------------------------------------------

install_coordinator() {
  local env_file=$ETC/coordinator.env
  DB_URL=${DB_URL:-$(env_get "$env_file" POS_DATABASE_URL)}
  ask DB_URL "Database URL (postgresql+psycopg://pos:PASSWORD@DB-HOST:5432/pos)"
  ask RAW_STORE "Raw store directory (NAS mount)" "/srv/pos/raw"
  ask BACKUP_DIR "Backup directory" "/srv/pos/backups"
  ask HTTP_PORT "HTTP port (point cloudflared here)" "8080"
  ask LISTEN "Listen on (127.0.0.1: this machine only; 0.0.0.0: also the LAN, e.g. for cloudflared in another LXC)" "127.0.0.1"
  [[ "$LISTEN" =~ ^[0-9.]+$|^[0-9a-fA-F:]+$ ]] || die "--listen must be an IP address, got: $LISTEN"
  if ! is_loopback "$LISTEN" && [[ -z "$TRUSTED_PROXY" ]] && can_prompt; then
    read -r -p "IP of your cloudflared host, whose visitor addresses are believed (empty: none): " \
      TRUSTED_PROXY </dev/tty || true
  fi
  local p
  for p in ${TRUSTED_PROXY//,/ }; do
    [[ "$p" =~ ^[0-9a-fA-F.:]+(/[0-9]{1,3})?$ ]] || die "--trusted-proxy: not an IP or CIDR: $p"
  done
  local admin_token
  admin_token=$(env_get "$env_file" POS_ADMIN_TOKEN)
  [[ -n "$admin_token" ]] || admin_token=$(random_secret)
  # Added by hand (docs/operations.md, "LLM"): kept across re-installs, never asked for
  # on the terminal.
  local anthropic_key
  anthropic_key=$(env_get "$env_file" POS_ANTHROPIC_API_KEY)
  # (write_env drops empty values: no placeholder line is written without a key)
  write_env "$env_file" "POS_DATABASE_URL=$DB_URL" "POS_ADMIN_TOKEN=$admin_token" \
    "POS_ANTHROPIC_API_KEY=$anthropic_key"
  # Separate subdirectories: pre-migration dumps belong to the coordinator's service
  # user (10001), nightly database dumps to postgres (all-in-one shares BACKUP_DIR).
  local coord_backups=$BACKUP_DIR/coordinator
  install -d -m 0755 "$BACKUP_DIR"
  install -d -o 10001 -g 10001 -m 0750 "$RAW_STORE" "$coord_backups"
  chown 10001:10001 "$RAW_STORE" "$coord_backups"  # an existing (NAS) directory too
  docker network create "$NET" >/dev/null 2>&1 || true
  for v in pos-coordinator-data pos-coordinator-exchange pos-agent-coordinator-data; do
    docker volume create "$v" >/dev/null
  done
  local agent; agent=$(agent_image pos-agent-coordinator)
  docker image inspect "$agent" >/dev/null 2>&1 || pull "$agent"
  run_container pos-agent-coordinator "$agent" --restart always --network "$NET" \
    --label org.price-of-size.agent=true \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v pos-agent-coordinator-data:/data -v pos-coordinator-exchange:/exchange \
    -v "$ETC:/etc/pos:ro" \
    -e POS_AGENT_ROLE=coordinator \
    -e "POS_AGENT_SELF_IMAGE=$agent" \
    -e POS_AGENT_CONTAINER_NAME=pos-agent-coordinator \
    -e POS_AGENT_AGENT_DATA_VOLUME=pos-agent-coordinator-data \
    -e POS_AGENT_EXCHANGE_VOLUME=pos-coordinator-exchange \
    -e POS_AGENT_SERVICE_ENV_FILE=/etc/pos/coordinator.env \
    -e "POS_AGENT_HOST_RAW_STORE=$RAW_STORE" \
    -e "POS_AGENT_HOST_BACKUP_DIR=$coord_backups" \
    -e "POS_AGENT_HTTP_PORT=$HTTP_PORT" \
    -e "POS_AGENT_HTTP_BIND=$LISTEN" \
    -e "POS_AGENT_TRUSTED_PROXIES=$TRUSTED_PROXY" \
    "${REGISTRY_ENV[@]}" --
  # The agent now pulls the images, migrates the database and starts the service
  # containers; the service answers through Caddy once it is up.
  info "starting the coordinator service (first start: images are pulled and the database is set up)"
  local started deadline=$((SECONDS + 600)) reported=""
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local begin=$SECONDS step=""
  while ((SECONDS < deadline)); do
    if curl -fsS --max-time 5 "$(local_url)/healthz" >/dev/null 2>&1; then
      [[ -t 2 ]] && printf '\r\033[K' >&2
      ok "coordinator service is up at $(local_url)"
      COORD_ADMIN_TOKEN=$admin_token
      return
    fi
    local logs err
    logs=$(docker logs --since "$started" pos-agent-coordinator 2>&1 || true)
    # the agent retries on its own; say what went wrong as soon as it does
    err=$(agent_error <<<"$logs" || true)
    if [[ -n "$err" && "$err" != "$reported" ]]; then
      [[ -t 2 ]] && printf '\r\033[K' >&2
      warn "the coordinator service failed to start (the agent keeps retrying):"
      echo "$err" | sed 's/^/    /' >&2
      reported=$err
    fi
    # what the agent is doing now (pulling, migrating, starting ...)
    step=$(grep -E ' INFO pos_agent' <<<"$logs" | tail -n 1 | sed -E 's/.* INFO pos_agent[^ ]* //' || true)
    if [[ -t 2 ]]; then
      printf '\r\033[K    %s ... %ds' "${step:-starting}" $((SECONDS - begin)) >&2
    fi
    sleep 3
  done
  [[ -t 2 ]] && printf '\r\033[K' >&2
  die "the coordinator service did not come up within 10 minutes; see: docker logs pos-agent-coordinator"
}

# The last error the agent logged: its message and the final line of the traceback.
agent_error() {
  awk '/ERROR pos_agent/ {msg = $0; last = ""; next}
       /^[A-Za-z_.]*(Error|Exception|Exit)[A-Za-z_.]*: / {last = $0}
       END {if (msg != "") {print msg; if (last != "") print last}}'
}
COORD_ADMIN_TOKEN=""

# --- updates through the coordinator ---------------------------------------------------
# The panel's Update button and `install.sh --update` share one implementation: the
# coordinator's update API (coordinator/pos_coordinator/updates.py).

COORD_API_ERROR="" COORD_API_CODE=""

# coord_api VAR METHOD PATH [JSON]: the response body in VAR; on an error returns 1
# with the reason in COORD_API_ERROR (no subshell, so the caller sees both)
coord_api() {
  local -n _body=$1
  local method=$2 path=$3 out code
  local args=(-sS --max-time 120 -X "$method" "$(local_url)$path"
    -H "Authorization: Bearer $COORD_ADMIN_TOKEN" -H 'content-type: application/json'
    -w '\n%{http_code}')
  [[ $# -ge 4 ]] && args+=(-d "$4")
  COORD_API_ERROR="" COORD_API_CODE="" _body=""
  if ! out=$(curl "${args[@]}" 2>&1); then
    COORD_API_ERROR=$out
    return 1
  fi
  code=${out##*$'\n'}
  out=${out%$'\n'*}
  COORD_API_CODE=$code
  if [[ $code != 2* ]]; then
    COORD_API_ERROR=$(jq -r '.detail // empty' <<<"$out" 2>/dev/null || true)
    COORD_API_ERROR=${COORD_API_ERROR:-HTTP $code}
    return 1
  fi
  _body=$out
}

# One line on where a rollout is (the panel shows the same steps).
ROLLOUT_LINE='
  def step(s): {"pulling": "downloading the new images", "selftest": "checking the new version",
    "preparing": "preparing", "backing_up": "backing up the database",
    "migrating": "migrating the database", "draining": "stopping the old version",
    "starting": "starting the new version", "health_check": "waiting until it answers",
    "agent_update": "updating its agent"}[s] // s;
  . as $o | (.rollouts[] | select(.id == $id)) as $r
  | if $r.status == "coordinator" then "coordinator: " + (
      if $o.coordinator_agent.update_target == $r.version and ($o.coordinator_agent.update_state // "idle") != "idle"
      then step($o.coordinator_agent.update_state) else "waiting for its agent" end)
    elif $r.status == "canary" then (
      [$o.nodes[] | select(.id == $r.canary_node_id)][0] as $n
      | "first node \($n.name): " + (if ($n.update_state // "idle") != "idle"
          then step($n.update_state) else "waiting for its agent" end))
    elif $r.status == "nodes" then
      "other nodes: \([$o.nodes[] | select(.managed and .version == $r.version and .running_version == $r.version)] | length) of \([$o.nodes[] | select(.managed)] | length) on \($r.version)"
    else $r.status end'

follow_update() {  # follow_update ROLLOUT_ID VERSION
  local id=$1 version=$2 begin=$SECONDS deadline=$((SECONDS + 3600)) last="" line status ov
  while ((SECONDS < deadline)); do
    if coord_api ov GET /api/admin/updates; then
      status=$(jq -r --arg id "$id" '.rollouts[] | select(.id == $id) | .status' <<<"$ov")
      line=$(jq -r --arg id "$id" "$ROLLOUT_LINE" <<<"$ov")
    else
      status="" line="coordinator: switching versions (not answering for a moment)"
    fi
    case "$status" in
      done)
        [[ -t 2 ]] && printf '\r\033[K' >&2
        ok "updated to $version: coordinator and nodes"
        return 0 ;;
      failed|cancelled)
        [[ -t 2 ]] && printf '\r\033[K' >&2
        local error
        error=$(jq -r --arg id "$id" '.rollouts[] | select(.id == $id) | .error // ""' <<<"$ov")
        [[ $status == cancelled ]] && { warn "the update to $version was stopped: $error"; return 1; }
        die "the update to $version failed: $error (the agents rolled back; docker logs pos-agent-coordinator)" ;;
    esac
    if [[ -t 2 ]]; then
      printf '\r\033[K    %s ... %ds' "$line" $((SECONDS - begin)) >&2
    elif [[ "$line" != "$last" ]]; then
      echo "    $line" >&2
    fi
    last=$line
    sleep 3
  done
  [[ -t 2 ]] && printf '\r\033[K' >&2
  warn "still updating after an hour; follow it on the panel's Releases page"
}

# A coordinator from before one-click updates (0.1.2 and older) has no update API:
# it is moved to the release this installer resolved the earlier way (register it,
# set coordinator.target_version, wait until it answers as that version).
legacy_coordinator_update() {
  local version body ov begin=$SECONDS deadline=$((SECONDS + 1800)) started step logs
  version=$(jq -r .version "$ETC/release.json")
  info "this coordinator predates one-click updates: moving it to $version first"
  body=$(jq -c '{version, images}' "$ETC/release.json")
  if ! coord_api ov POST /api/admin/releases "$body" && [[ $COORD_API_CODE != 409 ]]; then
    die "could not register $version: $COORD_API_ERROR"
  fi
  coord_api ov PUT /api/admin/settings/coordinator.target_version "{\"value\": \"$version\"}" \
    || die "could not set the coordinator's target version: $COORD_API_ERROR"
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  while ((SECONDS < deadline)); do
    if [[ $(curl -fsS --max-time 5 "$(local_url)/healthz" 2>/dev/null | jq -r '.version // empty' 2>/dev/null) == "$version" ]]; then
      [[ -t 2 ]] && printf '\r\033[K' >&2
      ok "coordinator runs $version"
      return 0
    fi
    logs=$(docker logs --since "$started" pos-agent-coordinator 2>&1 || true)
    if grep -q "update to $version failed" <<<"$logs"; then
      [[ -t 2 ]] && printf '\r\033[K' >&2
      die "the coordinator update to $version failed (rolled back): $(grep "update to $version failed" <<<"$logs" | tail -n 1)"
    fi
    step=$(grep -E ' INFO pos_agent' <<<"$logs" | tail -n 1 | sed -E 's/.* INFO pos_agent[^ ]* //' || true)
    [[ -t 2 ]] && printf '\r\033[K    coordinator: %s ... %ds' "${step:-waiting for its agent}" $((SECONDS - begin)) >&2
    sleep 3
  done
  die "the coordinator did not answer as $version within 30 minutes; see: docker logs pos-agent-coordinator"
}

update_installation() {
  COORD_ADMIN_TOKEN=${COORD_ADMIN_TOKEN:-$(env_get "$ETC/coordinator.env" POS_ADMIN_TOKEN)}
  if ! curl -fsS --max-time 5 "$(local_url)/healthz" >/dev/null 2>&1; then
    warn "the coordinator does not answer at $(local_url), so no update was started"
    return 1
  fi
  info "asking GitHub for the latest release"
  local ov id version
  if ! coord_api ov POST /api/admin/updates/check; then
    # no such route: 404, or 405 where the panel's catch-all GET route matches the path
    [[ $COORD_API_CODE == 404 || $COORD_API_CODE == 405 ]] || die "update check failed: $COORD_API_ERROR"
    legacy_coordinator_update
    # the new coordinator has the update API: the nodes follow through it
    coord_api ov POST /api/admin/updates/check || die "update check failed: $COORD_API_ERROR"
  fi
  local check_error; check_error=$(jq -r '.check.error // empty' <<<"$ov")
  if [[ -n "$check_error" ]]; then
    # the release asked for (--version, --release-file) does not need GitHub's latest
    ((EXPLICIT_RELEASE)) || die "update check: $check_error"
    warn "update check: $check_error (not needed for the release asked for)"
  fi
  # an update already running: follow it
  id=$(jq -r '[.rollouts[] | select(.status == "coordinator" or .status == "canary" or .status == "nodes")][0].id // empty' <<<"$ov")
  if [[ -n "$id" ]]; then
    version=$(jq -r --arg id "$id" '.rollouts[] | select(.id == $id) | .version' <<<"$ov")
    info "an update to $version is already running"
    follow_update "$id" "$version"
    return
  fi
  if ((EXPLICIT_RELEASE)); then
    # exactly the release asked for (--version, --release-file), not GitHub's latest;
    # registered first, so the coordinator does not need to find it on GitHub
    version=$(jq -r .version "$ETC/release.json")
    if ! coord_api ov POST /api/admin/releases "$(jq -c '{version, images}' "$ETC/release.json")" \
      && [[ $COORD_API_CODE != 409 ]]; then
      die "could not register $version: $COORD_API_ERROR"
    fi
  elif [[ $(jq -r .update_available <<<"$ov") == true ]]; then
    version=$(jq -r .check.latest.version <<<"$ov")
  else
    version=$(jq -r .current <<<"$ov")  # up to date: nodes behind still catch up
  fi
  if ! coord_api ov POST /api/admin/updates/start "{\"version\": \"$version\"}"; then
    if [[ "$COORD_API_ERROR" == "everything already runs"* ]]; then
      ok "up to date: everything runs $version"
      return
    fi
    die "could not start the update to $version: $COORD_API_ERROR"
  fi
  id=$(jq -r '.rollouts[0].id' <<<"$ov")
  info "updating to $version (coordinator first, then one node, then the others)"
  follow_update "$id" "$version"
}

# --- database ------------------------------------------------------------------------------

# The networks of the installer's own block in pg_hba.conf, comma-separated.
hba_networks() {  # hba_networks FILE
  [[ -r "$1" ]] || return 0
  sed -n '/# pos-install-begin/,/# pos-install-end/p' "$1" \
    | awk '$1 == "host" && $2 == "pos" && $3 == "pos" {print $4}' | paste -sd, -
}

pg_major() {
  local v
  for v in 17 16 15; do
    if apt-cache show "postgresql-$v" >/dev/null 2>&1 && apt-cache show "postgresql-$v-pgvector" >/dev/null 2>&1; then
      echo "$v"; return
    fi
  done
  die "no PostgreSQL 15-17 with pgvector available from this system's repositories"
}

install_database() {
  apt_update
  local v; v=$(pg_major)
  info "installing PostgreSQL $v with pgvector"
  apt_install "postgresql-$v" "postgresql-$v-pgvector" postgresql-contrib
  local cluster_dir="/etc/postgresql/$v/main"
  if [[ -n "$PG_DATA_DIR" ]]; then
    local current; current=$(su postgres -c "psql -Atc 'show data_directory'" 2>/dev/null || true)
    if [[ "$current" != "$PG_DATA_DIR" ]]; then
      local dbs; dbs=$(su postgres -c "psql -Atc \"select count(*) from pg_database where datname not in ('postgres','template0','template1')\"")
      [[ "$dbs" == 0 ]] || die "--pg-data-dir is only supported on a fresh cluster"
      info "moving the cluster to $PG_DATA_DIR"
      pg_dropcluster --stop "$v" main
      install -d -o postgres -g postgres -m 0700 "$PG_DATA_DIR"
      pg_createcluster -d "$PG_DATA_DIR" "$v" main >/dev/null
    fi
  fi
  local mem_kb shared cache
  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
  shared=$(( mem_kb / 4 / 1024 ))   # 25% of RAM, fixed at install time
  cache=$(( mem_kb * 3 / 4 / 1024 ))
  install -d "$cluster_dir/conf.d"
  # A repair keeps the networks set before: from install.conf, or (installations
  # from before it was saved there) from the installer's block in pg_hba.conf.
  [[ -n "$PG_ALLOW" ]] || PG_ALLOW=$(hba_networks "$cluster_dir/pg_hba.conf")
  ask PG_ALLOW "Networks allowed to connect (CIDR, comma-separated)" "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
  cat >"$cluster_dir/conf.d/pos.conf" <<EOF
# Written by install.sh (The Price of Size). Memory is sized once for this machine;
# PostgreSQL memory settings are fixed at start, so re-run the installer after resizing.
listen_addresses = '*'
shared_buffers = ${shared}MB
effective_cache_size = ${cache}MB
work_mem = 16MB
maintenance_work_mem = 512MB
max_connections = 100
wal_compression = on
max_wal_size = 4GB
checkpoint_completion_target = 0.9
random_page_cost = 1.1
effective_io_concurrency = 200
EOF
  chmod 0644 "$cluster_dir/conf.d/pos.conf"
  local hba="$cluster_dir/pg_hba.conf"
  sed -i '/# pos-install-begin/,/# pos-install-end/d' "$hba"
  {
    echo "# pos-install-begin"
    IFS=',' read -ra nets <<<"$PG_ALLOW"
    for n in "${nets[@]}"; do echo "host pos pos ${n// /} scram-sha-256"; done
    echo "# pos-install-end"
  } >>"$hba"
  systemctl enable --now "postgresql@$v-main" >/dev/null 2>&1 || true
  systemctl restart "postgresql@$v-main" 2>/dev/null || pg_ctlcluster "$v" main restart
  local pw_file=$ETC/database.env pw
  pw=$(env_get "$pw_file" POS_DB_PASSWORD)
  [[ -n "$pw" ]] || pw=$(random_secret)
  write_env "$pw_file" "POS_DB_PASSWORD=$pw"
  su postgres -c "psql -q -v ON_ERROR_STOP=1" <<EOF
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pos') THEN
    CREATE ROLE pos LOGIN PASSWORD '$pw';
  ELSE
    ALTER ROLE pos LOGIN PASSWORD '$pw';
  END IF;
END \$\$;
EOF
  # UTF8 whatever the system's locale: in a container without one (LANG unset) the
  # cluster's default is SQL_ASCII, which the coordinator cannot use. An earlier
  # install's empty SQL_ASCII database is made again; one with data is left alone.
  local enc objects
  enc=$(su postgres -c "psql -Atc \"SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = 'pos'\"")
  if [[ -n "$enc" && "$enc" != UTF8 ]]; then
    # empty means no user object at all (any schema; relations, functions, types,
    # large objects, schemas), apart from what an extension (vector) brings
    objects=$(su postgres -c "psql -d pos -At -v ON_ERROR_STOP=1" <<'SQL'
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid AND d.deptype = 'e'))
  + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                      AND d.objid = p.oid AND d.deptype = 'e'))
  + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_toast%' AND t.typrelid = 0 AND t.typelem = 0
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass
                      AND d.objid = t.oid AND d.deptype = 'e'))
  + (SELECT count(*) FROM pg_largeobject_metadata)
  + (SELECT count(*) FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'public')
      AND nspname NOT LIKE 'pg\_toast%' AND nspname NOT LIKE 'pg\_temp%');
SQL
)
    [[ "$objects" == 0 ]] || die "database pos uses $enc, not UTF8, and is not empty (docs/operations.md, Troubleshooting)"
    warn "database pos was created as $enc; it is empty and is created again as UTF8"
    su postgres -c "psql -q -v ON_ERROR_STOP=1 -c 'DROP DATABASE pos WITH (FORCE)'"
  fi
  su postgres -c "psql -q -v ON_ERROR_STOP=1" <<EOF
SELECT 'CREATE DATABASE pos OWNER pos ENCODING ''UTF8'' LC_COLLATE ''C.UTF-8'' LC_CTYPE ''C.UTF-8'' TEMPLATE template0'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'pos')\gexec
\c pos
CREATE EXTENSION IF NOT EXISTS vector;
EOF
  ok "PostgreSQL $v: database pos, role pos"
  install_db_backups "$v"
  DB_PASSWORD=$pw
}
DB_PASSWORD=""

install_db_backups() {
  local v=$1
  ask BACKUP_DIR "Backup directory" "/srv/pos/backups"
  local db_backups=$BACKUP_DIR/database
  install -d -m 0755 "$BACKUP_DIR"
  install -d -o postgres -g postgres -m 0750 "$db_backups"
  cat >/usr/local/sbin/pos-db-backup <<EOF
#!/bin/bash
# Nightly pg_dump of the pos database (custom format); keeps the newest 14.
set -euo pipefail
dir="$db_backups"
f="\$dir/pos-daily-\$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --compress=6 --file="\$f.partial" pos
mv "\$f.partial" "\$f"
ls -1t "\$dir"/pos-daily-*.dump | tail -n +15 | xargs -r rm --
echo "backup ok: \$f (\$(stat -c %s "\$f") bytes)"
EOF
  cat >/usr/local/sbin/pos-db-restore-test <<'EOF'
#!/bin/bash
# Monthly restore test: restores the newest backup into a scratch database,
# compares row counts of the raw layer, then drops the scratch database.
set -euo pipefail
latest=$(ls -1t "$1"/pos-daily-*.dump | head -1)
dropdb --if-exists pos_restore_test
createdb pos_restore_test
trap 'dropdb --if-exists pos_restore_test' EXIT
pg_restore --no-owner --dbname=pos_restore_test "$latest"
n=$(psql -Atd pos_restore_test -c "select count(*) from alembic_version")
[[ "$n" -ge 1 ]] || { echo "restore test FAILED: no schema version in $latest"; exit 1; }
echo "restore test ok: $latest"
EOF
  chmod 0755 /usr/local/sbin/pos-db-backup /usr/local/sbin/pos-db-restore-test
  cat >/etc/systemd/system/pos-db-backup.service <<'EOF'
[Unit]
Description=The Price of Size: nightly database backup
[Service]
Type=oneshot
User=postgres
ExecStart=/usr/local/sbin/pos-db-backup
EOF
  cat >/etc/systemd/system/pos-db-backup.timer <<'EOF'
[Unit]
Description=Nightly database backup
[Timer]
OnCalendar=*-*-* 03:17:00
RandomizedDelaySec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF
  cat >/etc/systemd/system/pos-db-restore-test.service <<EOF
[Unit]
Description=The Price of Size: monthly backup restore test
[Service]
Type=oneshot
User=postgres
ExecStart=/usr/local/sbin/pos-db-restore-test $db_backups
EOF
  cat >/etc/systemd/system/pos-db-restore-test.timer <<'EOF'
[Unit]
Description=Monthly backup restore test
[Timer]
OnCalendar=*-*-01 05:41:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
  if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
    systemctl enable --now pos-db-backup.timer pos-db-restore-test.timer >/dev/null
    ok "nightly backups to $db_backups, monthly restore test"
  else
    warn "no systemd: schedule /usr/local/sbin/pos-db-backup yourself"
  fi
}

# --- uninstall -----------------------------------------------------------------------------

uninstall_role() {  # uninstall_role node|coordinator
  local role=$1 agent components volumes env_file
  case $role in
    node) agent=pos-agent-worker; components="worker worker-selftest"
          volumes="pos-worker-state pos-worker-exchange pos-agent-worker-data"; env_file=worker.env ;;
    coordinator) agent=pos-agent-coordinator
          components="coordinator coordinator-selftest coordinator-backup coordinator-migrate caddy"
          volumes="pos-coordinator-data pos-coordinator-exchange pos-agent-coordinator-data pos-raw"
          env_file=coordinator.env ;;
  esac
  # The agent first: a running agent would recreate the containers it manages.
  local suffix c ids
  for suffix in "" -updater -next; do docker rm -f "$agent$suffix" >/dev/null 2>&1 || true; done
  [[ $role == node ]] && { docker rm -f pos-worker >/dev/null 2>&1 || true; }
  for c in $components; do
    ids=$(docker ps -aq --filter "label=org.price-of-size.component=$c" 2>/dev/null || true)
    # shellcheck disable=SC2086
    [[ -n "$ids" ]] && docker rm -f $ids >/dev/null
  done
  ok "removed $role containers"
  if [[ $PURGE -eq 1 ]]; then
    local v
    for v in $volumes; do
      docker volume inspect "$v" >/dev/null 2>&1 || continue
      docker volume rm "$v" >/dev/null 2>&1 || warn "could not remove volume $v (in use?)"
    done
    rm -f "$ETC/$env_file"
    ok "deleted $role data"
  fi
}

uninstall_database() {
  if [[ -f /etc/systemd/system/pos-db-backup.timer ]]; then
    systemctl disable --now pos-db-backup.timer pos-db-restore-test.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/pos-db-{backup,restore-test}.{service,timer}
    systemctl daemon-reload 2>/dev/null || true
  fi
  rm -f /usr/local/sbin/pos-db-backup /usr/local/sbin/pos-db-restore-test
  ok "removed database backup timers (the database itself is never deleted by this script)"
}

uninstall() {
  # Without --role: everything on this machine. With --role: only that role.
  local roles=$ROLE
  [[ -z "$roles" || "$roles" == all-in-one ]] && roles="node coordinator database"
  if [[ $PURGE -eq 1 ]]; then
    confirm "Delete the data of: $roles (identities, spool, signing key, config)?" \
      || die "not confirmed; nothing deleted"
  fi
  if ! command -v docker >/dev/null; then
    roles=${roles//node/}
    roles=${roles//coordinator/}
  fi
  local r
  for r in $roles; do
    case $r in
      node|coordinator) uninstall_role "$r" ;;
      database) uninstall_database ;;
    esac
  done
  if [[ $PURGE -eq 1 && "$roles" == *node*coordinator* ]]; then
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$ETC"
    warn "raw store, backups and the PostgreSQL database were NOT deleted"
  fi
  ok "uninstalled: $roles$([[ $PURGE -eq 1 ]] && echo ' (data purged)' || echo ' (data kept; --purge deletes it)')"
}

# A node on the coordinator's own machine: it joins the coordinator's Docker network
# and is enrolled with a code the installer creates itself.
install_local_node() {  # install_local_node LABEL
  if [[ -z "$ENROLL_CODE" ]] && ! vol_has pos-worker-state identity.json; then
    ENROLL_CODE=$(curl -fsS --max-time 30 -X POST "$(local_url)/api/admin/enrollment-codes" \
      -H "Authorization: Bearer $COORD_ADMIN_TOKEN" -H 'content-type: application/json' \
      -d "{\"label\":\"$1\"}" | jq -r .code)
    [[ -n "$ENROLL_CODE" && "$ENROLL_CODE" != null ]] || die "could not create an enrollment code"
  fi
  COORDINATOR_URL=local
  install_node
}

# Run without options where it is already installed: what to do.
installed_menu() {
  local version=${INSTALLED_VERSION:-?} ans n=0 choices=()
  echo "The Price of Size is installed here: $ROLE, release $version." >/dev/tty
  case "$ROLE" in
    coordinator|all-in-one)
      choices+=("update|Update        the latest release: this coordinator, then every node") ;;
    node)
      if [[ "${MODE:-agent}" == simple ]]; then
        choices+=("update|Update        the latest release for this node")
      fi ;;
  esac
  choices+=("repair|Repair        install again with the saved settings")
  if [[ "$ROLE" == coordinator && "$LOCAL_NODE" != 1 ]]; then
    choices+=("local-node|Add a node    collect data on this machine too")
  fi
  choices+=("uninstall|Uninstall     remove services, keep the data")
  choices+=("purge|Delete        remove services and delete their data (keys, identities, settings)")
  choices+=("cancel|Cancel")
  local c
  for c in "${choices[@]}"; do
    n=$((n + 1))
    printf '  %d) %s\n' "$n" "${c#*|}" >/dev/tty
  done
  if [[ "$ROLE" == node && "${MODE:-agent}" != simple ]]; then
    echo "  (this node is updated by its coordinator: Releases > Update in its panel)" >/dev/tty
  fi
  read -r -p "Choice [1]: " ans </dev/tty || true
  ans=${ans:-1}
  [[ "$ans" =~ ^[0-9]+$ ]] && ((ans >= 1 && ans <= n)) || die "unknown choice $ans"
  case "${choices[ans - 1]%%|*}" in
    update) ACTION=update ;;
    repair) NONINTERACTIVE=1 ;;
    local-node) LOCAL_NODE=1 NONINTERACTIVE=1 ;;
    uninstall) ACTION=uninstall ;;
    purge)
      local word
      echo "This deletes the node identities, the coordinator's signing key (nodes must enroll" >/dev/tty
      echo "again) and the settings in $ETC. The database, raw store and backups are kept." >/dev/tty
      read -r -p "Type delete to confirm: " word </dev/tty || true
      [[ "$word" == delete ]] || die "not confirmed; nothing deleted"
      ACTION=uninstall PURGE=1 ASSUME_YES=1 ;;
    cancel) exit 0 ;;
  esac
}

# --- main -----------------------------------------------------------------------------------

choose_role() {
  [[ -n "$ROLE" ]] && return
  if ! can_prompt; then ROLE=node; return; fi
  cat >/dev/tty <<'EOF'
Which role should this machine have?
  1) Node         collects data for a coordinator (default)
  2) Coordinator  API, planner and control panel
  3) Database     PostgreSQL + pgvector for the coordinator
  4) All-in-one   everything on this machine (testing, small setups)
EOF
  local ans
  read -r -p "Role [1]: " ans </dev/tty || true
  case "${ans:-1}" in
    1|node) ROLE=node ;; 2|coordinator) ROLE=coordinator ;;
    3|database) ROLE=database ;; 4|all-in-one) ROLE=all-in-one ;;
    *) die "unknown choice $ans" ;;
  esac
}

main() {
  need_root
  detect_os
  local explicit_role=$ROLE
  if [[ -n "$VERSION" || -n "$RELEASE_FILE" ]]; then EXPLICIT_RELEASE=1; fi
  load_conf
  # installed already, and nothing asked for: a menu
  if [[ $ACTION == install && -z "$explicit_role" && -n "$ROLE" ]] && can_prompt; then
    installed_menu
  fi
  if [[ $ACTION == uninstall ]]; then ROLE=$explicit_role; uninstall; return; fi
  if [[ $ACTION == update ]]; then
    [[ -n "$ROLE" ]] || die "nothing installed yet (no $CONF)"
    NONINTERACTIVE=1
  fi
  choose_role
  case "$ROLE" in node|coordinator|database|all-in-one) ;; *) die "unknown role $ROLE" ;; esac
  info "role: $ROLE ($ARCH, $OS_ID $OS_CODENAME)"
  ensure_base_tools

  if [[ "$ROLE" == database ]]; then
    install_database
    save_conf
    local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo
    ok "database ready. Coordinator database URL:"
    echo "    postgresql+psycopg://pos:$DB_PASSWORD@${ip:-THIS-HOST}:5432/pos"
    echo "  (password also in $ETC/database.env)"
    return
  fi

  ensure_docker
  docker_login
  registry_env
  resolve_release
  if [[ "$ROLE" != node ]]; then
    # updates from the panel do not touch install.conf: the running coordinator knows better
    local running
    running=$(curl -fsS --max-time 5 "$(local_url)/healthz" 2>/dev/null | jq -r '.version // empty' 2>/dev/null || true)
    INSTALLED_VERSION=${running:-$INSTALLED_VERSION}
  fi
  if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" != "$VERSION" ]]; then
    info "installed: $INSTALLED_VERSION, installing: $VERSION"
  fi
  case "$ROLE" in
    node) install_node ;;
    coordinator)
      install_coordinator
      if [[ "$LOCAL_NODE" == 1 ]]; then install_local_node local; fi ;;
    all-in-one)
      install_database
      docker network create "$NET" >/dev/null 2>&1 || true
      local gw; gw=$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Gateway}}')
      DB_URL=${DB_URL:-postgresql+psycopg://pos:$DB_PASSWORD@$gw:5432/pos}
      install_coordinator
      install_local_node all-in-one ;;
  esac
  save_conf
  if [[ $ACTION == update ]]; then
    case "$ROLE" in
      coordinator|all-in-one) update_installation ;;
      node) info "nodes are updated by their coordinator: its panel (Releases, Update) or install.sh --update there" ;;
    esac
  fi
  echo
  ok "The Price of Size $VERSION installed ($ROLE)"
  if [[ -n "$COORD_ADMIN_TOKEN" ]]; then
    echo "  Admin token: in $ETC/coordinator.env (POS_ADMIN_TOKEN)"
    if is_loopback "$LISTEN"; then
      echo "  Next: publish $(local_url) through a Cloudflare Tunnel (docs/operations.md)"
    else
      echo "  Control panel on the LAN: http://THIS-HOST:$HTTP_PORT (listening on $LISTEN)"
      echo "  Next: point your Cloudflare Tunnel's public hostname at http://THIS-HOST:$HTTP_PORT"
      [[ -n "$TRUSTED_PROXY" ]] \
        || echo "  (no --trusted-proxy: visitors through the tunnel show the cloudflared host's address)"
    fi
  fi
}

main "$@"
