#!/usr/bin/env bash
# Environment-only time qualification for the fixed Rocky lab.
# Execute on jumper 130 before any certificate-bearing Kubernetes setup.
set -euo pipefail

STATE=/var/tmp/kubeauto-upgrade-time-qualification
RECEIPT=${STATE}.json
HOSTS=(192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136)
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
fail() { echo "UPGRADE_TIME_QUALIFICATION_FAIL: $*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || fail "root is required"
[[ ! -e "$STATE.lock" ]] || fail "qualification already active"
printf '%s\n' "$$" > "$STATE.lock"
cleanup() { rm -f "$STATE.lock"; }
trap cleanup EXIT INT TERM

# The jumper creates certificates and is the time authority for this run.  It
# must be corrected before it is used as the comparison clock; otherwise a
# stale jumper can falsely reject healthy, NTP-synchronised source nodes.
systemctl restart chronyd
chronyc -a makestep >/dev/null
systemctl is-active --quiet chronyd

# The source NTP configuration is already present on the fixed lab nodes.
# Restart and makestep only clocks; no Kubernetes/runtime service is touched.
for host in "${HOSTS[@]}"; do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$host" \
    'systemctl restart chronyd && chronyc -a makestep >/dev/null && systemctl is-active --quiet chronyd'
done
control_epoch=$(date -u +%s)
"$(command -v python3 || command -v python)" - "$RECEIPT" "$control_epoch" "${HOSTS[@]}" <<'PY'
import json, subprocess, sys, time
from pathlib import Path
out = Path(sys.argv[1]); control = int(sys.argv[2]); hosts = sys.argv[3:]
rows=[]
for host in hosts:
    value=subprocess.check_output(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=no',f'root@{host}','date -u +%s'],text=True).strip()
    epoch=int(value); rows.append({'host':host,'epoch':epoch,'delta_seconds':epoch-control})
if any(abs(row['delta_seconds']) > 120 for row in rows):
    raise SystemExit('clock delta exceeds 120 seconds')
data={'schema_version':1,'scope':'fixed-lab-time-only','mutation':'chronyd-restart-and-makestep',
      'observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'control_epoch':control,'hosts':rows,
      'pass_marker':'UPGRADE_TIME_QUALIFICATION_PASS'}
tmp=out.with_suffix('.tmp'); tmp.write_text(json.dumps(data,indent=2)+'\n'); tmp.chmod(0o600); tmp.replace(out)
PY
chmod 0600 "$RECEIPT"
echo UPGRADE_TIME_QUALIFICATION_PASS
