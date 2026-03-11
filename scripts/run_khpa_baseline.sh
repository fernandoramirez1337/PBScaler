#!/usr/bin/env bash
# Run the KHPA baseline experiment end-to-end.
# Usage: bash scripts/run_khpa_baseline.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load shared GKE configuration
# shellcheck source=scripts/gke.env
source "${SCRIPT_DIR}/gke.env"

OUT_DIR="${PROJECT_ROOT}/results/khpa_baseline"
HPA_MANIFEST="${PROJECT_ROOT}/${BOUTIQUE_MANIFESTS}/hpa.yaml"

# ── Step 0: Prerequisites ────────────────────────────────────────────────────
echo "==> Step 0: Checking prerequisites"
for cmd in kubectl locust python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        exit 1
    fi
done
echo "    kubectl, locust, python3 — OK"

# ── Step 1: GKE credentials ──────────────────────────────────────────────────
echo "==> Step 1: Fetching GKE credentials"
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
    --zone "${ZONE}" --project "${PROJECT_ID}"

# ── Step 2: Apply HPAs ───────────────────────────────────────────────────────
echo "==> Step 2: Applying HPA manifest"
kubectl apply -f "${HPA_MANIFEST}" -n "${APP_NAMESPACE}"

# ── Step 3: Verify HPAs ──────────────────────────────────────────────────────
echo "==> Step 3: Verifying HPAs"
kubectl get hpa -n "${APP_NAMESPACE}"

# ── Step 4: Wait for frontend LoadBalancer IP ────────────────────────────────
echo "==> Step 4: Waiting for frontend-external LoadBalancer IP"
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

# ── Step 5: Create output directory ─────────────────────────────────────────
echo "==> Step 5: Creating output directory"
mkdir -p "${OUT_DIR}"

# ── Step 6: Record start time ────────────────────────────────────────────────
echo "==> Step 6: Recording start time"
START_TIME=$(date +%s)
echo "    START_TIME=${START_TIME}"

# ── Step 7: Start Prometheus port-forward ────────────────────────────────────
echo "==> Step 7: Starting Prometheus port-forward"
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PF_PID=$!
sleep 3  # Allow the port-forward to bind
echo "    Port-forward PID: ${PF_PID}"

# ── Step 8: Run Locust load test ─────────────────────────────────────────────
echo "==> Step 8: Running Locust load test (10 min)"
locust -f "${SCRIPT_DIR}/locustfile.py" --headless \
    --host "http://${FRONTEND_IP}" \
    --run-time 10m \
    --csv "${OUT_DIR}/locust" --csv-full-history \
    --loglevel WARNING || true  # continue even if locust exits non-zero

# ── Step 9: Record end time; restart port-forward ────────────────────────────
echo "==> Step 9: Recording end time"
END_TIME=$(date +%s)
echo "    END_TIME=${END_TIME}"

# Restart port-forward in case it died during the test
kill "${PF_PID}" 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PF_PID=$!
sleep 3

# ── Step 10: Collect metrics ─────────────────────────────────────────────────
echo "==> Step 10: Collecting metrics from Prometheus"
python3 "${SCRIPT_DIR}/collect_metrics.py" \
    --start "${START_TIME}" \
    --end   "${END_TIME}" \
    --namespace "${APP_NAMESPACE}" \
    --out "${OUT_DIR}"

# ── Step 11: Generate plots ──────────────────────────────────────────────────
echo "==> Step 11: Generating plots"
python3 "${SCRIPT_DIR}/plot_results.py" "${OUT_DIR}"

# ── Step 12: Cleanup and summary ─────────────────────────────────────────────
echo "==> Step 12: Cleanup"
kill "${PF_PID}" 2>/dev/null || true

echo ""
echo "==> Experiment complete."
echo "    Duration: $((END_TIME - START_TIME))s  (${START_TIME} → ${END_TIME})"
echo "    Output:   ${OUT_DIR}"
echo ""
echo "    Files generated:"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
