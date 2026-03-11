#!/usr/bin/env bash
# Run the PBScaler debug experiment end-to-end (with structured logging).
# Usage: bash scripts/run_pbscaler_debug.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load shared GKE configuration
# shellcheck source=scripts/gke.env
source "${SCRIPT_DIR}/gke.env"

OUT_DIR="${PROJECT_ROOT}/results/pbscaler_debug"

# ── Step 0: Prerequisites ────────────────────────────────────────────────────
echo "==> Step 0: Checking prerequisites"
for cmd in kubectl locust python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        exit 1
    fi
done
echo "    kubectl, locust, python3 — OK"

# Ensure 'schedule' is installed (PBScaler imports it)
pip3 install --quiet schedule

# ── Step 1: GKE credentials ──────────────────────────────────────────────────
echo "==> Step 1: Fetching GKE credentials"
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
    --zone "${ZONE}" --project "${PROJECT_ID}"

# ── Step 2: Delete any existing HPAs ─────────────────────────────────────────
echo "==> Step 2: Deleting existing HPAs (PBScaler manages scaling itself)"
kubectl delete hpa --all -n "${APP_NAMESPACE}" 2>/dev/null || true

# ── Step 3: Verify HPAs deleted ──────────────────────────────────────────────
echo "==> Step 3: Verifying HPAs are gone"
if kubectl get hpa -n "${APP_NAMESPACE}" 2>&1 | grep -q "No resources"; then
    echo "    No HPAs found — OK"
else
    kubectl get hpa -n "${APP_NAMESPACE}" || true
    echo "    WARNING: HPAs may still exist. PBScaler will conflict with them."
fi

# ── Step 4: Reset all deployments to 1 replica ──────────────────────────────
echo "==> Step 4: Resetting all deployments to 1 replica"
for deploy in $(kubectl get deployments -n "${APP_NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    # Skip loadgenerator and redis — not scaled by PBScaler
    case "${deploy}" in
        loadgenerator|redis-cart) continue ;;
    esac
    kubectl scale deployment "${deploy}" --replicas=1 -n "${APP_NAMESPACE}"
done
echo "    Waiting 30s for pods to stabilize..."
sleep 30

# ── Step 5: Wait for frontend LoadBalancer IP ────────────────────────────────
echo "==> Step 5: Waiting for frontend-external LoadBalancer IP"
FRONTEND_IP=""
for i in $(seq 1 30); do
    FRONTEND_IP=$(kubectl get svc frontend-external -n "${APP_NAMESPACE}" \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "${FRONTEND_IP}" ]]; then
        echo "    frontend IP: ${FRONTEND_IP}"
        break
    fi
    echo "    Waiting... (${i}/30)"
    sleep 10
done
if [[ -z "${FRONTEND_IP}" ]]; then
    echo "ERROR: Timed out waiting for frontend-external LoadBalancer IP"
    exit 1
fi

# ── Step 6: Create output directory ──────────────────────────────────────────
echo "==> Step 6: Creating output directory"
mkdir -p "${OUT_DIR}"

# ── Step 7: Start Prometheus port-forward ────────────────────────────────────
echo "==> Step 7: Starting Prometheus port-forward"
# Kill any existing port-forward on 9090
lsof -ti:9090 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PF_PID=$!
sleep 3
echo "    Port-forward PID: ${PF_PID}"

# ── Step 8: Record start time ────────────────────────────────────────────────
echo "==> Step 8: Recording start time"
START_TIME=$(date +%s)
echo "    START_TIME=${START_TIME}"

# ── Step 9: Start PBScaler controller in background ─────────────────────────
echo "==> Step 9: Starting PBScaler controller (background)"
cd "${PROJECT_ROOT}"
K8S_NAMESPACE="${APP_NAMESPACE}" python3 main.py 2>&1 | tee "${OUT_DIR}/pbscaler.log" &
PBSCALER_PID=$!
echo "    PBScaler PID: ${PBSCALER_PID}"

# Health check: wait 5s, then verify PBScaler is still alive
sleep 5
if ! kill -0 "${PBSCALER_PID}" 2>/dev/null; then
    echo "ERROR: PBScaler crashed on startup. Check ${OUT_DIR}/pbscaler.log"
    tail -30 "${OUT_DIR}/pbscaler.log" 2>/dev/null || true
    kill "${PF_PID}" 2>/dev/null || true
    exit 1
fi
echo "    PBScaler health check passed — still running after 5s"

# ── Step 10: Run Locust load test ────────────────────────────────────────────
echo "==> Step 10: Running Locust load test (10 min)"
locust -f "${SCRIPT_DIR}/locustfile.py" --headless \
    --host "http://${FRONTEND_IP}" \
    --run-time 10m \
    --csv "${OUT_DIR}/locust" --csv-full-history \
    --loglevel WARNING || true

# ── Step 11: Record end time; kill PBScaler + port-forward ───────────────────
echo "==> Step 11: Recording end time and stopping PBScaler"
END_TIME=$(date +%s)
echo "    END_TIME=${END_TIME}"

kill "${PBSCALER_PID}" 2>/dev/null || true
wait "${PBSCALER_PID}" 2>/dev/null || true
echo "    PBScaler stopped"

kill "${PF_PID}" 2>/dev/null || true
sleep 1

# ── Step 12: Restart port-forward for metric collection ─────────────────────
echo "==> Step 12: Restarting Prometheus port-forward"
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PF_PID=$!
sleep 3

# ── Step 13: Collect metrics ─────────────────────────────────────────────────
echo "==> Step 13: Collecting metrics from Prometheus"
python3 "${SCRIPT_DIR}/collect_metrics.py" \
    --start "${START_TIME}" \
    --end   "${END_TIME}" \
    --namespace "${APP_NAMESPACE}" \
    --out "${OUT_DIR}"

# ── Step 14: Generate per-experiment plots ───────────────────────────────────
echo "==> Step 14: Generating per-experiment plots"
python3 "${SCRIPT_DIR}/plot_results.py" "${OUT_DIR}"

# ── Step 15: Generate comparison plots ───────────────────────────────────────
echo "==> Step 15: Generating comparison plots (KHPA vs PBScaler)"
python3 "${SCRIPT_DIR}/plot_comparison.py" \
    --khpa-dir "${PROJECT_ROOT}/results/khpa_baseline" \
    --pbscaler-dir "${OUT_DIR}" \
    --out "${PROJECT_ROOT}/results/comparison"

# ── Step 16: Print key log lines ─────────────────────────────────────────────
echo "==> Step 16: PBScaler log summary"
if [[ -f "${OUT_DIR}/pbscaler.log" ]]; then
    echo "    Anomaly detections:"
    grep -c 'ANOMALY:.*abnormal calls out of' "${OUT_DIR}/pbscaler.log" 2>/dev/null | xargs -I{} echo "      {} anomaly checks logged"
    echo "    Scaling events:"
    grep 'SCALE:' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -20 | sed 's/^/      /'
    echo "    GA optimizations:"
    grep 'GA_OPT: GA result' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -10 | sed 's/^/      /'
    echo "    PageRank analyses:"
    grep 'PAGERANK: Selected roots' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -10 | sed 's/^/      /'
    echo "    Waste detections:"
    grep 'WASTE: Waste roots' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -10 | sed 's/^/      /'
fi

# ── Step 17: Cleanup and summary ─────────────────────────────────────────────
echo "==> Step 17: Cleanup"
kill "${PF_PID}" 2>/dev/null || true

echo ""
echo "==> Experiment complete."
echo "    Duration: $((END_TIME - START_TIME))s  (${START_TIME} -> ${END_TIME})"
echo "    Output:   ${OUT_DIR}"
echo ""
echo "    PBScaler results:"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
echo ""
echo "    Comparison charts:"
ls -1 "${PROJECT_ROOT}/results/comparison" 2>/dev/null | sed 's/^/      /' || echo "      (none)"
