#!/usr/bin/env bash
# install.sh — idempotent setup for claude-router + claude-integ.
#
#   git clone https://github.com/Iwancof/claude-integ ~/somewhere/claude-integ
#   cd ~/somewhere/claude-integ && ./install.sh
#
# What it does (safe to re-run):
#   1. dependency check (python3 >= 3.11, jq, curl, claude)
#   2. seed ~/.config/claude-router/config.toml + integ.conf from the examples
#      (never overwrites existing files; config.toml is chmod 600)
#   3. install the systemd user service with ExecStart pointing at this clone,
#      then enable --now and health-check it (skipped if systemd is absent)
#   4. symlink claude-integ into ~/.local/bin
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

CONF_DIR="$HOME/.config/claude-router"
UNIT_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
ROUTER_URL="http://127.0.0.1:8399"

echo "== dependency check =="
missing=0
need() {
  if command -v "$1" >/dev/null 2>&1; then printf '  ok    %s\n' "$1"
  else printf '  MISS  %s — %s\n' "$1" "$2" >&2; missing=1; fi
}
need python3 "python 3.11+ (stdlib only)"
need jq      "needed by claude-integ for the /model picker cache"
need curl    "needed for router health checks"
need claude  "Claude Code CLI (https://claude.com/claude-code), logged in"
if [ "$missing" -ne 0 ]; then
  echo "ERROR: required dependencies missing; aborting." >&2
  exit 1
fi
pyver="$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')"
if [ "$pyver" != "True" ]; then
  echo "ERROR: python3 >= 3.11 required (tomllib)." >&2
  exit 1
fi

echo
echo "== config seeding ($CONF_DIR) =="
mkdir -p "$CONF_DIR"
if [ -e "$CONF_DIR/config.toml" ]; then
  echo "  keep  config.toml (already exists)"
else
  install -m 600 config.example.toml "$CONF_DIR/config.toml"
  echo "  new   config.toml  <- EDIT THIS: put your vendor API keys in it"
fi
if [ -e "$CONF_DIR/integ.conf" ]; then
  echo "  keep  integ.conf (already exists)"
else
  install -m 644 integ.conf.example "$CONF_DIR/integ.conf"
  echo "  new   integ.conf (all defaults commented out — edit to taste)"
fi

echo
echo "== systemd user service =="
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  # The repo file is a template; rewrite @REPO@ to this clone's absolute path.
  # rm first in case an old install symlinked the repo file directly.
  rm -f "$UNIT_DIR/claude-router.service"
  sed "s|@REPO@|$REPO|" claude-router.service > "$UNIT_DIR/claude-router.service"
  systemctl --user daemon-reload
  systemctl --user enable --now claude-router.service
  sleep 1
  if curl -sf -m 3 "$ROUTER_URL/claude-router/health" >/dev/null; then
    echo "  ok    claude-router active + healthy at $ROUTER_URL"
  else
    echo "  WARN  service enabled but health check failed — inspect:" >&2
    echo "        journalctl --user -u claude-router -n 20" >&2
  fi
else
  echo "  skip  no systemd user session — run the router manually:"
  echo "        python3 $REPO/claude_router.py --config $CONF_DIR/config.toml"
fi

echo
echo "== launcher symlink =="
mkdir -p "$BIN_DIR"
ln -sfn "$REPO/claude-integ" "$BIN_DIR/claude-integ"
echo "  ok    $BIN_DIR/claude-integ -> $REPO/claude-integ"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  WARN  $BIN_DIR is not in PATH" ;;
esac

echo
echo "Done. Next steps:"
echo "  1. edit $CONF_DIR/config.toml (vendor endpoints/API keys; delete backends you don't use)"
echo "  2. systemctl --user restart claude-router   # after config edits"
echo "  3. claude-integ                             # or: claude-integ kimi / glm / sol ..."
