#!/usr/bin/env bash
# Lifecycle test of install.sh on a Docker host (destructive for pos-* resources).
#
#   RELEASE_FILE=release.json DB_URL='postgresql+psycopg://pos:pos@{gw}:5432/pos_inst' \
#     sudo -E deploy/test/install_test.sh
#
# {gw} is replaced by the gateway of the pos-net network (the Docker host).
set -Eeuo pipefail
cd "$(dirname "$0")/../.."
: "${RELEASE_FILE:?set RELEASE_FILE}" "${DB_URL:?set DB_URL}"
PORT=${PORT:-8090}
DATA=${DATA:-/srv/pos-install-test}
I=./install.sh

step() { echo; echo "### $*"; }
fail() { echo "FAIL: $*" >&2; docker ps -a >&2; exit 1; }
wait_for() {  # wait_for SECONDS DESCRIPTION COMMAND...
  local t=$1 what=$2; shift 2
  for _ in $(seq 1 "$t"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "timed out: $what"
}
admin() { curl -fsS -H "Authorization: Bearer $(sed -n 's/^POS_ADMIN_TOKEN=//p' /etc/pos/coordinator.env)" "$@"; }
node_online() { admin "http://127.0.0.1:$PORT/api/admin/nodes" | jq -e '[.[] | select(.online)] | length == 1'; }
jobs_done() { admin "http://127.0.0.1:$PORT/api/admin/jobs/stats" | jq -e 'all(.status == "done")'; }

$I --uninstall --purge --yes >/dev/null 2>&1 || true
docker network create pos-net >/dev/null 2>&1 || true
GW=$(docker network inspect pos-net -f '{{(index .IPAM.Config 0).Gateway}}')
DB=${DB_URL//\{gw\}/$GW}

step "coordinator role"
$I --role coordinator --non-interactive --release-file "$RELEASE_FILE" --db-url "$DB" \
  --raw-store "$DATA/raw" --backup-dir "$DATA/backups" --http-port "$PORT"
curl -fsS "http://127.0.0.1:$PORT/healthz" | jq -e .ok
[[ "$(stat -c %u "$DATA/raw")" == 10001 ]] || fail "raw store not owned by the service user"
[[ "$(stat -c %u "$DATA/backups/coordinator")" == 10001 ]] || fail "coordinator backups not owned by 10001"
bind=$(docker inspect -f '{{(index (index .HostConfig.PortBindings "80/tcp") 0).HostIp}}' pos-caddy)
[[ "$bind" == 127.0.0.1 ]] || fail "Caddy must publish on loopback only (got $bind)"

step "node role (agent mode)"
CODE=$(admin -X POST -H 'content-type: application/json' -d '{"label":"t"}' \
  "http://127.0.0.1:$PORT/api/admin/enrollment-codes" | jq -r .code)
$I --role node --non-interactive --release-file "$RELEASE_FILE" \
  --coordinator "http://$GW:$PORT" --enroll "$CODE" --name install-test
wait_for 90 "node online" node_online
admin -X POST -H 'content-type: application/json' \
  -d '{"jobs":[{"type":"echo"},{"type":"dummy_fetch","payload":{"url":"https://t.invalid/"}}]}' \
  "http://127.0.0.1:$PORT/api/admin/jobs" >/dev/null
wait_for 60 "jobs done" jobs_done
[[ "$(stat -c %a /etc/pos/worker.env)" == 600 ]] || fail "worker.env must be 0600"

step "re-running is a no-op for a healthy node"
before=$(docker inspect -f '{{.State.StartedAt}}' pos-worker-a)
$I --role node --non-interactive --release-file "$RELEASE_FILE" --coordinator "http://$GW:$PORT"
[[ "$(docker inspect -f '{{.State.StartedAt}}' pos-worker-a)" == "$before" ]] || fail "worker was restarted"

step "a config change reaches the running worker"
$I --role node --non-interactive --release-file "$RELEASE_FILE" --coordinator "http://$GW:$PORT" \
  --name renamed-node
wait_for 60 "worker recreated with the new name" bash -c \
  "docker inspect -f '{{.Config.Env}}' pos-worker-a pos-worker-b 2>/dev/null | grep -q POS_WORKER_NAME=renamed-node"
wait_for 90 "node online after the config change" node_online

if [[ -n "${OLD_RELEASE_FILE:-}" ]]; then
  step "an older release.json never downgrades a running agent"
  agent_before=$(docker inspect -f '{{.Config.Image}}' pos-agent-worker)
  $I --role node --non-interactive --release-file "$OLD_RELEASE_FILE" --coordinator "http://$GW:$PORT" \
    --name renamed-node
  [[ "$(docker inspect -f '{{.Config.Image}}' pos-agent-worker)" == "$agent_before" ]] \
    || fail "the agent was downgraded"
fi

step "simple mode and back"
$I --role node --mode simple --non-interactive --release-file "$RELEASE_FILE" --coordinator "http://$GW:$PORT"
docker inspect pos-agent-worker >/dev/null 2>&1 && fail "agent still present in simple mode"
docker inspect pos-worker-a >/dev/null 2>&1 && fail "agent-managed worker still present in simple mode"
wait_for 60 "simple-mode worker online" node_online

step "uninstall keeps data; reinstall needs no new enrollment code"
$I --uninstall --role node >/dev/null
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || fail "node uninstall touched the coordinator"
docker volume inspect pos-worker-state >/dev/null || fail "worker identity volume was deleted"
$I --role node --mode agent --non-interactive --release-file "$RELEASE_FILE" --coordinator "http://$GW:$PORT"
wait_for 90 "node online again with the same identity" node_online
[[ "$(admin "http://127.0.0.1:$PORT/api/admin/nodes" | jq length)" == 1 ]] || fail "re-enrolled as a new node"

step "purge"
$I --uninstall --purge --yes
[[ -z "$(docker ps -aq --filter label=org.price-of-size.managed=true)" ]] || fail "containers left"
[[ ! -d /etc/pos ]] || fail "/etc/pos left"
echo; echo "INSTALL TEST PASSED"
