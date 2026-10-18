#!/usr/bin/env bash
set -euo pipefail

# deploy_pi.sh - Deploy the committed HEAD to a TDM install that runs as systemd
# user services (the Raspberry Pi setup) and check that it came back up.
#
# Usage: scripts/deploy_pi.sh [ssh_host]        (default: pi-redmitools)
#
# Environment:
#   TDM_DEPLOY_DIR       install dir on the host        (default: ~/tdm)
#   TDM_DEPLOY_SERVICES  user services to restart       (default: "tdm tdm2")
#   DRY_RUN=1            print the steps, change nothing
#
# What it does:
#   1. Packages HEAD (src web lang scripts main.py pyproject.toml) with git archive
#      - uncommitted changes are NOT deployed
#   2. Uploads it to <host>:/tmp/tdm-deploy.tar
#   3. On the host: backs the current code up to ~/tdm-backup-<old version>.tar,
#      unpacks the new code (data/ and the venv are untouched), notes how long each
#      instance log is, restarts the services
#   4. Waits until every instance log reports the new version AFTER the restart,
#      then shows what each instance logged since

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOST="${1:-pi-redmitools}"
DIR="${TDM_DEPLOY_DIR:-~/tdm}"
SERVICES="${TDM_DEPLOY_SERVICES:-tdm tdm2}"
FILES="src web lang scripts main.py pyproject.toml"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/version.py)"

run() {
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "+ $*"
  else
    "$@"
  fi
}

# Runs a script (stdin) on the host; in a dry run, prints it instead.
remote() {
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "+ ssh $HOST bash -s <<"
    cat
  else
    ssh "$HOST" bash -s
  fi
}

if [ -n "$(git status --porcelain)" ]; then
  echo "Note: uncommitted changes are not deployed (HEAD only)." >&2
fi

ARCHIVE="$(mktemp -t tdm-deploy.XXXXXX)"
trap 'rm -f "$ARCHIVE"' EXIT

echo "Deploying $VERSION ($(git rev-parse --short HEAD)) to $HOST:$DIR"
# shellcheck disable=SC2086
run git archive HEAD $FILES -o "$ARCHIVE"
run scp -q "$ARCHIVE" "$HOST:/tmp/tdm-deploy.tar"

remote <<EOF
set -e
cd $DIR
old=\$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/version.py)
tar -cf ~/tdm-backup-\$old.tar $FILES
tar -xf /tmp/tdm-deploy.tar
rm -f /tmp/tdm-deploy.tar
for f in logs/TDM*.log; do echo "\$f \$(wc -l < \$f)"; done > /tmp/tdm-deploy-offsets
systemctl --user restart $SERVICES
echo "updated \$old -> $VERSION"
EOF

remote <<EOF
cd $DIR
since() { off=\$(awk -v f="\$1" '\$1 == f {print \$2}' /tmp/tdm-deploy-offsets); tail -n +\$((\${off:-0} + 1)) "\$1"; }
logs=\$(ls logs/TDM*.log)
for i in \$(seq 1 60); do
  pending=0
  for f in \$logs; do since "\$f" | grep -q 'Version: $VERSION' || pending=1; done
  [ \$pending = 0 ] && break
  sleep 5
done
systemctl --user is-active $SERVICES
for f in \$logs; do
  echo "== \$f (since restart)"
  since "\$f" | grep -E 'Version: |Wanted games:|Watch sent OK|ERROR|Traceback' | tail -4 | cut -c1-200
done
EOF
