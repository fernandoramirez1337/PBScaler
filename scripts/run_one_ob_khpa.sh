#!/usr/bin/env bash
# Run one Online Boutique experiment under Kubernetes HPA and stash outputs in
# results/sprint-1/online-boutique-khpa/<workload>/run<rep>/.
#
# Sprint 1.5 Fase L driver. Caller responsibilities:
#   - Cluster `pbscaler-experiment` already up (setup_gke.sh ran)
#
# Args:
#   $1 = workload  (step|bursty|diurnal|steady_ramp|trace_driven)
#   $2 = rep       (1|2|3)
#   $3 = duration  (seconds)
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 <workload> <rep> <duration_s>" >&2
    exit 1
fi

WORKLOAD="$1"
REP="$2"
DURATION="$3"
SEED=$((42 + REP * 100))

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"

LOCUSTFILE_ABS="${REPO_ROOT}/benchmarks/online_boutique/locustfile_${WORKLOAD}.py"
PHANTOM_SCRIPT="${REPO_ROOT}/instrumentation/measure_phantom_capacity.py"
OUT_DIR="${REPO_ROOT}/results/sprint-1/online-boutique-khpa/${WORKLOAD}/run${REP}"

if [[ ! -f "${LOCUSTFILE_ABS}" ]]; then
    echo "ERROR: locustfile not found: ${LOCUSTFILE_ABS}" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
echo "==> run_one_ob_khpa: workload=${WORKLOAD} rep=${REP} duration=${DURATION}s seed=${SEED}"
echo "    out_dir=${OUT_DIR}"

# Kick off phantom_capacity in background
PHANTOM_DURATION=$((DURATION + 120))
python3 "${PHANTOM_SCRIPT}" \
    --namespace online-boutique \
    --duration "${PHANTOM_DURATION}" \
    --interval 5 \
    --out "${OUT_DIR}/phantom_capacity.csv" &
PHANTOM_PID=$!
echo "    phantom_capacity PID: ${PHANTOM_PID}"

# Watchdog — force-kill locust DURATION+120s after start
WATCHDOG_AFTER=$((DURATION + 120))
(
    sleep "${WATCHDOG_AFTER}"
    if pgrep -f "locust.*locustfile_${WORKLOAD}" >/dev/null; then
        echo "[watchdog] killing locust after ${WATCHDOG_AFTER}s grace period" >&2
        pkill -f "locust.*locustfile_${WORKLOAD}" 2>/dev/null || true
    fi
) &
WATCHDOG_PID=$!

# Run KHPA baseline. Best-effort: tolerate non-zero exit (Step 16 grep, etc).
RUN_EXIT=0
LOCUSTFILE="${LOCUSTFILE_ABS}" \
LOCUST_SEED="${SEED}" \
LOCUST_RUN_TIME="${DURATION}s" \
    bash "${SCRIPT_DIR}/run_khpa_baseline.sh" || RUN_EXIT=$?
if [[ ${RUN_EXIT} -ne 0 ]]; then
    echo "WARNING: run_khpa_baseline.sh exited ${RUN_EXIT} — recovering data products" >&2
fi

kill "${PHANTOM_PID}" 2>/dev/null || true
wait "${PHANTOM_PID}" 2>/dev/null || true

# Move khpa baseline outputs into the run dir (best effort)
SRC="${PROJECT_ROOT}/results/khpa_baseline"
if [[ -d "${SRC}" ]]; then
    mv "${SRC}"/* "${OUT_DIR}/" 2>/dev/null || true
    rmdir "${SRC}" 2>/dev/null || true
fi

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "${OUT_DIR}/metadata.json" <<EOF
{
  "benchmark": "online-boutique",
  "controller": "khpa",
  "workload": "${WORKLOAD}",
  "rep": ${REP},
  "seed": ${SEED},
  "duration_s": ${DURATION},
  "git_sha": "${GIT_SHA}",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "slo_ms": 500,
  "hpa_cpu_target_pct": 80,
  "hpa_max_replicas": 5,
  "run_khpa_exit_code": ${RUN_EXIT},
  "cluster": "pbscaler-experiment",
  "machine_type": "e2-standard-4",
  "num_nodes": 3,
  "disk_size_gb": 50
}
EOF

echo ""
echo "==> Run ${REP} of workload ${WORKLOAD} (KHPA) complete."
ls -1 "${OUT_DIR}" | sed 's/^/      /'
