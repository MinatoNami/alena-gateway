#!/usr/bin/env bash
#
# alena-gateway — deploy to the host that fronts the tailnet origin.
#
#   ./deploy/deploy.sh              sync, build, start, wire nginx, verify
#   ./deploy/deploy.sh nginx        reinstall the site and reload, nothing else
#   ./deploy/deploy.sh routes       check services.yaml and the nginx site agree
#   ./deploy/deploy.sh test         run the status service's unit tests
#   ./deploy/deploy.sh verify       probe every route end to end
#   ./deploy/deploy.sh status       container and endpoint health
#   ./deploy/deploy.sh rollback     list images; rollback <tag> to pin one
#   ./deploy/deploy.sh logs         tail the status service
#
# Idempotent throughout: re-running is the normal way to ship a change.
#
# The target host is not committed, because it names a specific machine:
#
#   echo my-ssh-host > deploy/.deploy-host        # untracked
#
# or pass --host, or export ALENA_GATEWAY_HOST.

set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR="alena-gateway"
NGINX_SITE="alena-gateway"
SERVE_PORT=443

# ---------------------------------------------------------------------------- 
# Output
# ---------------------------------------------------------------------------- 
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; RESET=''
fi
step() { printf '\n%s==>%s %s%s%s\n' "$BOLD" "$RESET" "$BOLD" "$1" "$RESET"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
info() { printf '    %s%s%s\n' "$DIM" "$1" "$RESET"; }
die()  { printf '\n    %s✗%s %s\n\n' "$RED" "$RESET" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------- 
# Target
# ---------------------------------------------------------------------------- 
# The origin names a specific machine, so it lives in .env rather than in the
# tracked registry. services.yaml keeps a placeholder only so a fresh checkout
# parses; anything that actually talks to the deployment uses this.
ORIGIN="${GATEWAY_ORIGIN:-}"
if [ -z "$ORIGIN" ] && [ -f "$LOCAL_DIR/.env" ]; then
  ORIGIN="$(grep -E '^GATEWAY_ORIGIN=' "$LOCAL_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]')"
fi

SSH_HOST="${ALENA_GATEWAY_HOST:-}"
ACTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) SSH_HOST="${2:-}"; shift 2 ;;
    --host=*) SSH_HOST="${1#*=}"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      if [ -z "$ACTION" ]; then ACTION="$1"; else ROLLBACK_TAG="$1"; fi
      shift ;;
  esac
done
ACTION="${ACTION:-deploy}"

if [ -z "$SSH_HOST" ] && [ -f "$LOCAL_DIR/deploy/.deploy-host" ]; then
  SSH_HOST="$(tr -d '[:space:]' < "$LOCAL_DIR/deploy/.deploy-host")"
fi
# Checked lazily, so the purely local actions (`routes`) work anywhere. No
# default on purpose: a deploy script that guesses which machine to touch
# eventually touches the wrong one.
require_host() {
  [ -n "$SSH_HOST" ] || die "no target host. Write one to deploy/.deploy-host, pass --host, or set ALENA_GATEWAY_HOST."
}

require_origin() {
  [ -n "$ORIGIN" ] || die "no origin. Copy .env.example to .env and set GATEWAY_ORIGIN, or export it."
}

remote() { require_host; ssh -o ConnectTimeout=10 "$SSH_HOST" "$@"; }

# ---------------------------------------------------------------------------- 
# routes — services.yaml and the nginx site must agree
# ---------------------------------------------------------------------------- 
#
# The registry drives the status page and the nginx site drives the traffic. If
# they drift, the page cheerfully links to a path that 404s. This is the cheap
# check that catches it before a deploy does.
check_routes() {
  step "Routes"
  local conf="$LOCAL_DIR/nginx/$NGINX_SITE.conf"
  local missing=0 prefix

  while IFS= read -r prefix; do
    [ -n "$prefix" ] || continue
    if grep -qE "location [~^= ]*\^?~? ?${prefix//\//\\/}" "$conf"; then
      ok "$prefix"
    else
      warn "$prefix has no matching location in nginx/$NGINX_SITE.conf"
      missing=$((missing + 1))
    fi
  done < <(grep -E '^\s+prefix:' "$LOCAL_DIR/services.yaml" | awk '{print $2}')

  # Root-level paths an app owns outright. Same failure mode, opposite
  # direction: forget one and the gateway swallows it into the status page.
  while IFS= read -r prefix; do
    [ -n "$prefix" ] || continue
    if grep -qE "location [~^= ]*\^?~? ?${prefix//\//\\/}" "$conf"; then
      ok "$prefix (reserved)"
    else
      warn "$prefix is reserved in services.yaml but not routed"
      missing=$((missing + 1))
    fi
  done < <(grep -E '^\s+reserves:' "$LOCAL_DIR/services.yaml" | sed 's/.*\[//; s/\].*//' | tr ',' '\n' | tr -d ' ')

  [ "$missing" -eq 0 ] || die "$missing route(s) declared in services.yaml are not served by nginx"
  ok "services.yaml and the nginx site agree"
}

# ---------------------------------------------------------------------------- 
# test — the status service's own unit tests
# ---------------------------------------------------------------------------- 
#
# Local, fast, and no network: the probes run against an httpx MockTransport, so
# this asserts on the classification, the cache and the API surface without
# touching a deployment.
run_tests() {
  step "Tests"

  local py="python3"
  [ -x "$LOCAL_DIR/backend/.venv/bin/python" ] && py="$LOCAL_DIR/backend/.venv/bin/python"

  if ! "$py" -m pytest --version >/dev/null 2>&1; then
    # Not fatal: a deploy from a machine without the dev dependencies should
    # still work. But say so plainly — tests that quietly stop running are
    # worse than no tests, because the green output implies they passed.
    warn "pytest not available, so the unit tests DID NOT RUN"
    warn "  python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt"
    TESTS_SKIPPED=1
    return
  fi

  ( cd "$LOCAL_DIR/backend" && "$py" -m pytest -q ) || die "tests failed; nothing was deployed"
  ok "unit tests pass"
}

# ---------------------------------------------------------------------------- 
# Preflight
# ---------------------------------------------------------------------------- 
preflight() {
  step "Preflight"
  require_host
  require_origin
  ok "origin $ORIGIN"
  remote true 2>/dev/null || die "cannot ssh to $SSH_HOST"
  ok "ssh to $SSH_HOST"

  remote "command -v docker >/dev/null" || die "docker is not installed on $SSH_HOST"
  remote "docker compose version >/dev/null 2>&1" || die "docker compose v2 is not available on $SSH_HOST"
  ok "docker and compose v2"

  remote "command -v nginx >/dev/null" || die "nginx is not installed on $SSH_HOST (apt install nginx)"
  ok "nginx $(remote 'nginx -v 2>&1' | sed 's/.*nginx\///')"

  remote "command -v tailscale >/dev/null" || die "tailscale is not installed on $SSH_HOST"
  ok "tailscale"
}

# ---------------------------------------------------------------------------- 
# Sync and start
# ---------------------------------------------------------------------------- 
sync_source() {
  step "Sync"
  remote "mkdir -p ~/$REMOTE_DIR"
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.env' \
    --exclude 'deploy/.deploy-host' \
    --exclude '__pycache__/' \
    --exclude '.venv/' \
    "$LOCAL_DIR/" "$SSH_HOST:$REMOTE_DIR/"
  ok "source synced to ~/$REMOTE_DIR"

  # .env is excluded above so the server's copy is never clobbered by a local
  # one. Seed it on first deploy only.
  if ! remote "test -f ~/$REMOTE_DIR/.env"; then
    remote "cp ~/$REMOTE_DIR/.env.example ~/$REMOTE_DIR/.env && chmod 600 ~/$REMOTE_DIR/.env"
    ok "seeded .env from .env.example"
    return
  fi

  # A setting added to .env.example after the first deploy never reaches the
  # server, because the file it seeds is only written once. Silent defaults are
  # how a machine ends up configured differently from what the repo describes.
  # GATEWAY_ORIGIN is excluded: the step below writes it from this machine's
  # .env, so warning that it is absent and then setting it reads as a fault.
  local missing
  missing="$(remote "
    for key in \$(grep -oE '^[A-Z_]+=' ~/$REMOTE_DIR/.env.example | tr -d '=' | grep -v '^GATEWAY_ORIGIN\$'); do
      grep -qE \"^\$key=\" ~/$REMOTE_DIR/.env || printf '%s ' \"\$key\"
    done")"
  if [ -n "$missing" ]; then
    warn "in .env.example but not in the server's .env: $missing"
    warn "compose defaults apply; add them to ~/$REMOTE_DIR/.env to be explicit"
  else
    ok ".env has every key .env.example declares"
  fi

  # The container reads this at start, including after a reboot, so it has to be
  # on the server rather than only in this shell's environment.
  if ! remote "grep -qxF 'GATEWAY_ORIGIN=$ORIGIN' ~/$REMOTE_DIR/.env"; then
    remote "sed -i '/^GATEWAY_ORIGIN=/d' ~/$REMOTE_DIR/.env && printf 'GATEWAY_ORIGIN=%s\n' '$ORIGIN' >> ~/$REMOTE_DIR/.env"
    ok "set GATEWAY_ORIGIN on the server"
  fi
}

start_stack() {
  step "Status service"
  local tag
  tag="$(git -C "$LOCAL_DIR" rev-parse --short HEAD 2>/dev/null || echo latest)"
  remote "cd ~/$REMOTE_DIR && GATEWAY_IMAGE_TAG=$tag docker compose up -d --build" >/dev/null
  ok "built and started (tag $tag)"

  local port
  port="$(remote "grep -E '^GATEWAY_PORT=' ~/$REMOTE_DIR/.env | cut -d= -f2" 2>/dev/null || true)"
  port="${port:-8090}"

  local waited=0
  until remote "curl -fsS -m3 http://127.0.0.1:$port/api/healthz >/dev/null 2>&1"; do
    waited=$((waited + 2))
    [ "$waited" -lt 40 ] || die "status service did not answer on 127.0.0.1:$port within 40s"
    sleep 2
  done
  ok "answering on 127.0.0.1:$port"
}

# ---------------------------------------------------------------------------- 
# nginx
# ---------------------------------------------------------------------------- 
configure_nginx() {
  step "nginx"

  remote "sudo install -d -m 755 /etc/nginx/snippets"
  remote "sudo install -m 644 ~/$REMOTE_DIR/nginx/snippets/alena-gateway-proxy.conf /etc/nginx/snippets/alena-gateway-proxy.conf"
  remote "sudo install -m 644 ~/$REMOTE_DIR/nginx/$NGINX_SITE.conf /etc/nginx/sites-available/$NGINX_SITE"
  remote "sudo ln -sfn /etc/nginx/sites-available/$NGINX_SITE /etc/nginx/sites-enabled/$NGINX_SITE"

  # Sites that bound 0.0.0.0:443 before the gateway existed. `tailscale serve`
  # needs that port on the tailnet address, and a wildcard listener holds every
  # address on the box — so these have to stand down before it can take it.
  #
  #   health-exporter  superseded outright; the gateway routes health-app now
  #   alena-voice      LAN-only site, upstreams not running
  #
  # Disabled, not deleted: both stay in sites-available, so re-enabling either
  # is one symlink. See README, "The sites this replaced".
  local site
  for site in health-exporter alena-voice; do
    if remote "test -e /etc/nginx/sites-enabled/$site"; then
      remote "sudo rm -f /etc/nginx/sites-enabled/$site"
      ok "retired $site (still in sites-available)"
    fi
  done

  local test_output
  if ! test_output="$(remote 'sudo nginx -t' 2>&1)"; then
    remote "sudo rm -f /etc/nginx/sites-enabled/$NGINX_SITE"
    # Report the failing run's output. Re-testing after the rollback would print
    # a success message for the config that was just removed.
    printf "%s\n" "$test_output" | sed 's/^/    /'
    die "nginx rejected the site; it has been removed and nothing was reloaded"
  fi
  ok "config valid"

  remote "sudo systemctl reload nginx"
  ok "nginx reloaded"
}

# ---------------------------------------------------------------------------- 
# tailscale serve
# ---------------------------------------------------------------------------- 
#
# This is what gives the origin a real certificate without opening a port to any
# other network. It is set once and survives reboots; re-running is harmless.
configure_serve() {
  step "tailscale serve"

  local nginx_port
  nginx_port="$(remote "grep -E '^GATEWAY_NGINX_PORT=' ~/$REMOTE_DIR/.env | cut -d= -f2" 2>/dev/null || true)"
  nginx_port="${nginx_port:-8088}"

  # nginx has just been reloaded, so anything still on 443 that is not tailscaled
  # is a listener the gateway does not know about — and it will block the bind.
  # tailscaled itself holding 443 is the desired end state, not a conflict, so a
  # re-run of this script must not trip over its own work.
  local holder
  holder="$(remote "sudo ss -tlnp 'sport = :443' 2>/dev/null | tail -n +2 | grep -v tailscaled" || true)"
  if [ -n "$holder" ]; then
    printf '%s\n' "$holder" | sed 's/^/      /'
    die "something other than tailscaled still listens on :443. Retire that listener first."
  fi
  ok ":443 is clear of other listeners"

  # The listener LumaIndex used before the gateway existed. Left in place it
  # would keep serving luma at a base path it no longer answers on.
  if remote "sudo tailscale serve status --json 2>/dev/null | grep -q '\"8443\"'"; then
    remote "sudo tailscale serve --https=8443 off" >/dev/null
    ok "removed the old :8443 listener"
  fi

  local current
  current="$(remote "sudo tailscale serve status 2>/dev/null" || true)"

  if printf '%s' "$current" | grep -q "127.0.0.1:$nginx_port"; then
    ok "already serving :$SERVE_PORT -> 127.0.0.1:$nginx_port"
  else
    if [ -n "$current" ]; then
      info "current serve config:"
      printf '%s\n' "$current" | sed 's/^/      /'
    fi
    remote "sudo tailscale serve --bg --https=$SERVE_PORT http://127.0.0.1:$nginx_port" >/dev/null
    ok "serving :$SERVE_PORT -> 127.0.0.1:$nginx_port"
  fi
}

# ---------------------------------------------------------------------------- 
# Verify
# ---------------------------------------------------------------------------- 
verify() {
  step "Verify"

  require_origin
  local origin="$ORIGIN"

  # Probed from this machine, over the tailnet, through TLS — the same path a
  # browser and the iOS app take. Checking from the server would skip both
  # `tailscale serve` and the certificate.
  local failures=0
  check() {
    local path="$1" expect="$2" label="$3" code
    code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$origin$path" || echo 000)"
    if printf '%s' "$expect" | tr ',' '\n' | grep -qx "$code"; then
      ok "$label  $path -> $code"
    else
      warn "$label  $path -> $code (wanted $expect)"
      failures=$((failures + 1))
    fi
  }

  # Location must be relative. An absolute one would carry nginx's own
  # plaintext :8088 listener, sending the browser to an address that exists
  # only inside the host — which is exactly what happened the first time.
  check_relative_redirect() {
    local path="$1" want="$2" location
    location="$(curl -s -o /dev/null -m 15 -D - "$origin$path" 2>/dev/null | awk 'tolower($1)=="location:"{print $2}' | tr -d '\r')"
    if [ "$location" = "$want" ]; then
      ok "redirect     $path -> $location"
    else
      warn "redirect     $path -> '$location' (wanted '$want')"
      failures=$((failures + 1))
    fi
  }

  check /                    200         "status page"
  check /api/status          200         "status JSON"
  check /healthz             200         "health-app liveness"
  # A 200 here proves /v1 is routed: it is a session probe that answers
  # {"authenticated": false} rather than refusing, which is what lets the
  # dashboard decide whether to show a login screen.
  check /v1/auth/me          200         "health-app session probe"
  # And this proves the gateway did not accidentally authenticate anyone on the
  # way through — it is a real data endpoint behind a real permission check.
  check /v1/analytics/overview 401,403   "health-app API refuses anonymous"
  check /health-app/         200         "health-app dashboard"
  # Prefixed, not root. Both of these answered at the origin root until
  # health-app grew a per-request SCRIPT_NAME; a regression would put them back
  # there and quietly re-collide with the next Django app on this origin.
  check /health-app/admin/   200,302     "health-app admin (prefixed)"
  check /admin/              404         "root /admin is free"
  check /athena/             200,302     "athena"
  check /luma-index/         200,302     "luma-index"

  check_relative_redirect /athena     /athena/
  check_relative_redirect /luma-index /luma-index/
  check_relative_redirect /dashboard/ /health-app/

  [ "$failures" -eq 0 ] || die "$failures route(s) did not answer as expected"
  ok "every route answered"

  # Routing correctly is not the same as reporting correctly. The first deploy
  # passed every check above while the status page showed all three services
  # down, because the probes could not reach loopback from inside a bridge
  # network. Assert on what the page actually says.
  step "Probes"
  local down
  down="$(curl -fsS -m 20 "$origin/api/status" \
    | python3 -c "import json,sys; print(' '.join(s['id'] for s in json.load(sys.stdin)['services'] if s['status'] != 'up'))")"
  if [ -n "$down" ]; then
    curl -fsS -m 20 "$origin/api/status" | python3 -c "$(cat <<'PY'
import json, sys
for service in json.load(sys.stdin)["services"]:
    if service["status"] != "up":
        print("    {} -> {}".format(service["id"], service["detail"]))
PY
)"
    die "the status page reports these down: $down"
  fi
  ok "every service probes up"
}

# ---------------------------------------------------------------------------- 
# rollback — put a previous status-service image back
# ---------------------------------------------------------------------------- 
#
# Scope is deliberately narrow, and the script says so rather than implying it
# reverted everything. The status service is stateless, so re-running an older
# image is a complete rollback *of that service*. The nginx site is not in the
# image: it is installed from the working tree, so reverting a routing mistake
# means checking the file out and reinstalling it. Both are printed below.
rollback() {
  require_host

  local target="${1:-}"
  local available
  available="$(remote "docker images alena-gateway-status --format '{{.Tag}}\t{{.CreatedAt}}'" | grep -v '<none>')"

  if [ -z "$available" ]; then
    die "no alena-gateway-status images on $SSH_HOST to roll back to"
  fi

  if [ -z "$target" ]; then
    step "Images on $SSH_HOST"
    printf '%s\n' "$available" | sed 's/^/    /'
    printf '\n    Pick one:  ./deploy/deploy.sh rollback <tag>\n'
    printf '    The nginx site is separate:\n'
    printf '      git checkout <tag> -- nginx/ services.yaml && ./deploy/deploy.sh nginx\n\n'
    return
  fi

  printf '%s\n' "$available" | awk '{print $1}' | grep -qx "$target" \
    || die "no image tagged '$target' on $SSH_HOST. Run rollback with no argument to list them."

  step "Rollback"
  warn "this reverts the status service image only, not the nginx site"
  remote "cd ~/$REMOTE_DIR && GATEWAY_IMAGE_TAG=$target docker compose up -d --no-build" >/dev/null
  ok "status service pinned to $target"

  local port waited=0
  port="$(remote "grep -E '^GATEWAY_PORT=' ~/$REMOTE_DIR/.env | cut -d= -f2" 2>/dev/null || true)"
  port="${port:-8090}"
  until remote "curl -fsS -m3 http://127.0.0.1:$port/api/healthz >/dev/null 2>&1"; do
    waited=$((waited + 2))
    [ "$waited" -lt 40 ] || die "the rolled-back image did not answer within 40s"
    sleep 2
  done
  ok "answering on 127.0.0.1:$port"

  verify
}

status() {
  step "Containers"
  remote "cd ~/$REMOTE_DIR && docker compose ps" || true

  step "Upstreams"
  require_origin
  local origin="$ORIGIN" json

  if ! json="$(curl -fsS -m 15 "$origin/api/status")"; then
    warn "status endpoint unreachable at $origin/api/status"
    return
  fi

  # Rendered by python rather than jq: jq is not installed on every machine this
  # might be run from, and python3 is.
  printf '%s' "$json" | python3 -c "$(cat <<'PY'
import json
import sys

report = json.load(sys.stdin)
for service in report["services"]:
    mark = "up  " if service["status"] == "up" else "DOWN"
    if service["status"] == "up":
        tail = "{}ms".format(service["latency_ms"])
    else:
        tail = service["detail"] or "unknown"
    print("    {} {:<18} :{:<5} {}".format(mark, service["name"], service["port"], tail))
    for part in service["components"]:
        mark = "up  " if part["status"] == "up" else "DOWN"
        print("      {} {:<16} :{}".format(mark, part["name"], part["port"]))
PY
)"
}

# ---------------------------------------------------------------------------- 
case "$ACTION" in
  deploy)
    check_routes
    run_tests
    preflight
    sync_source
    start_stack
    configure_nginx
    configure_serve
    verify
    step "Done"
    [ -n "${TESTS_SKIPPED:-}" ] && warn "reminder: the unit tests did not run for this deploy"
    printf '\n    %s\n\n' "$ORIGIN"
    ;;
  routes) check_routes ;;
  test)   run_tests ;;
  nginx)  sync_source; configure_nginx ;;
  verify) verify ;;
  status) status ;;
  rollback) rollback "${ROLLBACK_TAG:-}" ;;
  logs)   remote "cd ~/$REMOTE_DIR && docker compose logs -f --tail=100 status" ;;
  *)      die "unknown action: $ACTION (try deploy, nginx, routes, test, verify, status, rollback, logs)" ;;
esac
