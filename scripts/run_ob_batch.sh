#!/usr/bin/env bash
# Sprint 1 Fase G — Online Boutique batch runner.
# Iterates the workload × rep matrix on an already-up cluster.
#
# Workloads and durations match the plan (PLAN_CLAUDE_CODE.md, decisiones humano):
#   step          1800 s (30 min)
#   bursty        1800 s (30 min)
#   diurnal       7200 s (2 h compresión 12×)
#   steady_ramp   3600 s (60 min)
#
# Run1 of step is skipped if results/sprint-1/online-boutique/step/run1/
# already has metadata.json (the smoke ran already).
#
# Each individual run is best-effort; failures are logged but the batch
# continues. Final summary printed at the end.
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
        OUT_DIR="${REPO_ROOT}/results/sprint-1/online-boutique/${WORKLOAD}/run${REP}"
        if [[ -f "${OUT_DIR}/metadata.json" && -f "${OUT_DIR}/instances.csv" ]]; then
            echo "==> SKIP ${WORKLOAD}/run${REP}: already complete"
            SUMMARY_OK+=("${WORKLOAD}/run${REP} (skipped, prior data)")
            continue
        fi

        echo ""
        echo "##############################################################"
        echo "##  ${WORKLOAD} rep ${REP} — duration ${DURATION}s"
        echo "##  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "##############################################################"

        if bash "${SCRIPT_DIR}/run_one_ob_run.sh" "${WORKLOAD}" "${REP}" "${DURATION}"; then
            SUMMARY_OK+=("${WORKLOAD}/run${REP}")
        else
            SUMMARY_FAIL+=("${WORKLOAD}/run${REP}")
            echo "WARNING: ${WORKLOAD}/run${REP} did not complete cleanly — continuing"
            # Defensive cleanup of stale port-forwards / phantom_capacity processes
            pkill -f measure_phantom_capacity.py 2>/dev/null || true
            pkill -f "kubectl port-forward" 2>/dev/null || true
            sleep 5
        fi
    done
done

echo ""
echo "=============================================================="
echo "  Batch complete. $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="
echo "  OK (${#SUMMARY_OK[@]}):"
for r in "${SUMMARY_OK[@]}"; do echo "    $r"; done
echo "  FAIL (${#SUMMARY_FAIL[@]}):"
for r in "${SUMMARY_FAIL[@]}"; do echo "    $r"; done
