#!/usr/bin/env bash
# Run one Train Ticket (DOKS) experiment end-to-end and stash outputs in
# results/sprint-1/train-ticket/<workload>/run<rep>/.
#
# Sprint 1B Fase G driver. Caller responsibilities:
#   - DOKS cluster `pbscaler-train-ticket` already up (setup_doks_train_ticket.sh ran)
#
# Args:
#   $1 = workload  (step|bursty)
#   $2 = rep       (1|2|3)
#   $3 = duration  (seconds, e.g. 1800 for step/bursty)
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

LOCUSTFILE_ABS="${REPO_ROOT}/benchmarks/train_ticket/locustfile_${WORKLOAD}.py"
PHANTOM_SCRIPT="${REPO_ROOT}/instrumentation/measure_phantom_capacity.py"
OUT_DIR="${REPO_ROOT}/results/sprint-1/train-ticket/${WORKLOAD}/run${REP}"

if [[ ! -f "${LOCUSTFILE_ABS}" ]]; then
    echo "ERROR: locustfile not found: ${LOCUSTFILE_ABS}" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
echo "==> run_one_tt_run: workload=${WORKLOAD} rep=${REP} duration=${DURATION}s seed=${SEED}"
echo "    out_dir=${OUT_DIR}"

# Pre-flight reset BEFORE starting phantom_capacity so the kubectl stampede
# (43 deployments scaling to 1 simultaneously) does not saturate the DOKS API
# server while phantom is sampling. Previously this lived in Step 5 of
# run_pbscaler_doks_train_ticket.sh AFTER phantom started, which caused phantom
# to lose all queries beyond ~140s of a 1800s run. SKIP_RESET=1 below tells the
# DOKS script to skip its own redundant reset.
echo "==> Pre-flight: resetting all train-ticket deployments to 1 replica"
for deploy in $(kubectl get deployments -n train-ticket -o jsonpath='{.items[*].metadata.name}'); do
    kubectl scale deployment "${deploy}" --replicas=1 -n train-ticket >/dev/null
done
echo "    Pre-flight done. Waiting 90s for Java pods to stabilise..."
sleep 90

# Phantom capacity in background. Run a bit longer than the load so we
# capture trailing scale-down activity.
PHANTOM_DURATION=$((DURATION + 120))
python3 "${PHANTOM_SCRIPT}" \
    --namespace train-ticket \
    --duration "${PHANTOM_DURATION}" \
    --interval 5 \
    --out "${OUT_DIR}/phantom_capacity.csv" &
PHANTOM_PID=$!
echo "    phantom_capacity PID: ${PHANTOM_PID} (will run ${PHANTOM_DURATION}s)"

# Watchdog — force-kill locust if it doesn't terminate cleanly. Same fix
# as run_one_ob_run.sh — locust 2.43.4 may keep users running after the
# LoadShape returns None.
WATCHDOG_AFTER=$((DURATION + 120))
(
    sleep "${WATCHDOG_AFTER}"
    if pgrep -f "locust.*locustfile_${WORKLOAD}" >/dev/null; then
        echo "[watchdog] killing locust after ${WATCHDOG_AFTER}s grace period" >&2
        pkill -f "locust.*locustfile_${WORKLOAD}" 2>/dev/null || true
    fi
) &
WATCHDOG_PID=$!
echo "    watchdog PID: ${WATCHDOG_PID} (fires at +${WATCHDOG_AFTER}s)"

# Run PBScaler DOKS TT script with parameterised env vars.
# Tolerate non-zero exit (Step 16 grep tripping set -e is a known false-positive).
# TT_BASE_USERS default at the wrapper is 1200 (Cap_4 keff baseline, validated
# in run99 calibration on 2026-05-08). The locustfile module
# benchmarks/train_ticket/_common.py keeps default=120 for Sprint 1 reproducibility;
# the wrapper default is the Cap_4 intentional value. SKIP_RESET=1 because we
# already reset deployments in the pre-flight above.
RUN_EXIT=0
LOCUSTFILE="${LOCUSTFILE_ABS}" \
LOCUST_SEED="${SEED}" \
LOCUST_RUN_TIME="${DURATION}s" \
TT_BASE_USERS="${TT_BASE_USERS:-1200}" \
SKIP_RESET=1 \
    bash "${SCRIPT_DIR}/run_pbscaler_doks_train_ticket.sh" || RUN_EXIT=$?
if [[ ${RUN_EXIT} -ne 0 ]]; then
    echo "WARNING: run_pbscaler_doks_train_ticket.sh exited ${RUN_EXIT} — recovering data products" >&2
fi

kill "${PHANTOM_PID}" 2>/dev/null || true
wait "${PHANTOM_PID}" 2>/dev/null || true

# Move PBScaler outputs into the run dir (best effort).
SRC="${PROJECT_ROOT}/results/pbscaler_train_ticket"
if [[ -d "${SRC}" ]]; then
    mv "${SRC}"/* "${OUT_DIR}/" 2>/dev/null || true
    rmdir "${SRC}" 2>/dev/null || true
fi

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "${OUT_DIR}/metadata.json" <<EOF
{
  "benchmark": "train-ticket",
  "workload": "${WORKLOAD}",
  "rep": ${REP},
  "seed": ${SEED},
  "duration_s": ${DURATION},
  "git_sha": "${GIT_SHA}",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "slo_ms": 500,
  "slo_default_upstream": 200,
  "run_pbscaler_exit_code": ${RUN_EXIT},
  "cluster": "pbscaler-train-ticket-doks",
  "cloud": "DigitalOcean",
  "node_size": "s-4vcpu-8gb",
  "num_nodes": 4
}
EOF

echo ""
echo "==> Run ${REP} of workload ${WORKLOAD} complete."
echo "    Files in ${OUT_DIR}:"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
