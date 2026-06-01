#!/usr/bin/env bash
# Run one Train Ticket experiment under Kubernetes HPA on DOKS and stash
# outputs in results/sprint-1/train-ticket-khpa/<workload>/run<rep>/.
#
# Sprint 1.5 Fase K driver. Caller responsibilities:
#   - DOKS cluster `pbscaler-train-ticket` already up
#   - KUBECONFIG points to the DOKS kubeconfig (e.g. ~/.kube/config_tt)
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
OUT_DIR="${REPO_ROOT}/results/sprint-1/train-ticket-khpa/${WORKLOAD}/run${REP}"

if [[ ! -f "${LOCUSTFILE_ABS}" ]]; then
    echo "ERROR: locustfile not found: ${LOCUSTFILE_ABS}" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
echo "==> run_one_tt_khpa: workload=${WORKLOAD} rep=${REP} duration=${DURATION}s seed=${SEED}"
echo "    out_dir=${OUT_DIR}"

PHANTOM_DURATION=$((DURATION + 120))
python3 "${PHANTOM_SCRIPT}" \
    --namespace train-ticket \
    --duration "${PHANTOM_DURATION}" \
    --interval 5 \
    --out "${OUT_DIR}/phantom_capacity.csv" &
PHANTOM_PID=$!
echo "    phantom_capacity PID: ${PHANTOM_PID}"

WATCHDOG_AFTER=$((DURATION + 120))
(
    sleep "${WATCHDOG_AFTER}"
    if pgrep -f "locust.*locustfile_${WORKLOAD}" >/dev/null; then
        echo "[watchdog] killing locust after ${WATCHDOG_AFTER}s grace period" >&2
        pkill -f "locust.*locustfile_${WORKLOAD}" 2>/dev/null || true
    fi
) &
WATCHDOG_PID=$!

RUN_EXIT=0
LOCUSTFILE="${LOCUSTFILE_ABS}" \
LOCUST_SEED="${SEED}" \
LOCUST_RUN_TIME="${DURATION}s" \
    bash "${SCRIPT_DIR}/run_khpa_doks_train_ticket.sh" || RUN_EXIT=$?
if [[ ${RUN_EXIT} -ne 0 ]]; then
    echo "WARNING: run_khpa_doks_train_ticket.sh exited ${RUN_EXIT} — recovering data products" >&2
fi

kill "${PHANTOM_PID}" 2>/dev/null || true
wait "${PHANTOM_PID}" 2>/dev/null || true

SRC="${PROJECT_ROOT}/results/khpa_train_ticket"
if [[ -d "${SRC}" ]]; then
    mv "${SRC}"/* "${OUT_DIR}/" 2>/dev/null || true
    rmdir "${SRC}" 2>/dev/null || true
fi

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "${OUT_DIR}/metadata.json" <<EOF
{
  "benchmark": "train-ticket",
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
  "cluster": "pbscaler-train-ticket-doks",
  "cloud": "DigitalOcean",
  "node_size": "s-4vcpu-8gb",
  "num_nodes": 6
}
EOF

echo ""
echo "==> Run ${REP} of workload ${WORKLOAD} (KHPA TT) complete."
ls -1 "${OUT_DIR}" | sed 's/^/      /'
