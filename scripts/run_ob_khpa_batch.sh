#!/usr/bin/env bash
# Sprint 1.5 Fase L — KHPA Online Boutique batch.
# 5 workloads × 3 reps = 15 runs on the existing GKE cluster.
# Skip-logic: a run is done if metadata.json + instances.csv both exist.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

WORKLOADS=(
    "step:1800"
    "bursty:1800"
    "diurnal:14400"
    "steady_ramp:3600"
    "trace_driven:3600"
)
REPS=(1 2 3)

declare -a SUMMARY_OK=()
declare -a SUMMARY_FAIL=()

for entry in "${WORKLOADS[@]}"; do
    WORKLOAD="${entry%%:*}"
    DURATION="${entry##*:}"
    for REP in "${REPS[@]}"; do
        OUT_DIR="${REPO_ROOT}/results/sprint-1/online-boutique-khpa/${WORKLOAD}/run${REP}"
        if [[ -f "${OUT_DIR}/metadata.json" && -f "${OUT_DIR}/instances.csv" ]]; then
            echo "==> SKIP OB-KHPA ${WORKLOAD}/run${REP}: already complete"
            SUMMARY_OK+=("${WORKLOAD}/run${REP} (skipped)")
            continue
        fi

        echo ""
        echo "##############################################################"
        echo "##  OB-KHPA ${WORKLOAD} rep ${REP} — duration ${DURATION}s"
        echo "##  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "##############################################################"

        if bash "${SCRIPT_DIR}/run_one_ob_khpa.sh" "${WORKLOAD}" "${REP}" "${DURATION}"; then
            SUMMARY_OK+=("${WORKLOAD}/run${REP}")
        else
            SUMMARY_FAIL+=("${WORKLOAD}/run${REP}")
            pkill -f measure_phantom_capacity.py 2>/dev/null || true
            pkill -f "kubectl port-forward" 2>/dev/null || true
            sleep 5
        fi
    done
done

echo ""
echo "=============================================================="
echo "  OB-KHPA batch complete. $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="
echo "  OK (${#SUMMARY_OK[@]}):"
for r in "${SUMMARY_OK[@]:-}"; do [[ -n "$r" ]] && echo "    $r"; done
echo "  FAIL (${#SUMMARY_FAIL[@]}):"
for r in "${SUMMARY_FAIL[@]:-}"; do [[ -n "$r" ]] && echo "    $r"; done
