#!/usr/bin/env bash
# Run Kubernetes HPA baseline against the Train Ticket benchmark on DOKS.
#
# Same shape as run_pbscaler_doks_train_ticket.sh but:
#   - No PBScaler controller. HPA is reactive Kubernetes-native scaling.
#   - Applies others/HPA/train-ticket.sh to create HPA per service.
#   - Cleans up HPAs at the end.
#
# Honoured env vars (parity with run_pbscaler_*):
#   LOCUSTFILE      path to a locustfile (default: scripts/locustfile.py)
#   LOCUST_RUN_TIME duration string (default: 10m)
#   LOCUST_SEED     integer seed exported for the locustfile RNG
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/do.env
source "${SCRIPT_DIR}/do.env"

OUT_DIR="${PROJECT_ROOT}/results/khpa_train_ticket"
HPA_SCRIPT="${PROJECT_ROOT}/others/HPA/${APP_NAMESPACE}.sh"

# ── Step 0: Prerequisites ────────────────────────────────────────────────────
echo "==> Step 0: Checking prerequisites"
for cmd in kubectl locust python3 doctl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        exit 1
    fi
done
if [[ ! -x "${HPA_SCRIPT}" ]] && [[ ! -f "${HPA_SCRIPT}" ]]; then
    echo "ERROR: HPA shell script not found: ${HPA_SCRIPT}"
    exit 1
fi
echo "    kubectl, locust, python3, doctl, ${HPA_SCRIPT##*/} — OK"
pip3 install --quiet schedule pyyaml

# ── Step 1: DOKS credentials ─────────────────────────────────────────────────
echo "==> Step 1: Fetching DOKS credentials"
doctl kubernetes cluster kubeconfig save "${CLUSTER_NAME}"

# ── Step 2: Delete any existing HPAs (clean slate) ──────────────────────────
echo "==> Step 2: Deleting any existing HPAs"
kubectl delete hpa --all -n "${APP_NAMESPACE}" 2>/dev/null || true

# ── Step 3: Reset all deployments to 1 replica ──────────────────────────────
echo "==> Step 3: Resetting all deployments to 1 replica"
for deploy in $(kubectl get deployments -n "${APP_NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    kubectl scale deployment "${deploy}" --replicas=1 -n "${APP_NAMESPACE}"
done
echo "    Waiting 60s for Java pods to stabilise..."
sleep 60

# ── Step 4: Apply HPAs ──────────────────────────────────────────────────────
echo "==> Step 4: Applying HPAs (kubectl autoscale per service)"
bash "${HPA_SCRIPT}"
echo "    HPAs created:"
kubectl get hpa -n "${APP_NAMESPACE}" --no-headers | wc -l | xargs -I{} echo "      {} HPA resources"

# ── Step 5: Locate Train Ticket frontend (port-forward) ──────────────────────
echo "==> Step 5: Locating Train Ticket frontend"
UI_SVC="ts-ui-dashboard"
if ! kubectl get svc "${UI_SVC}" -n "${APP_NAMESPACE}" &>/dev/null; then
    echo "ERROR: service ${UI_SVC} not found in namespace ${APP_NAMESPACE}"
    exit 1
fi
lsof -ti:8081 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${APP_NAMESPACE}" "svc/${UI_SVC}" 8081:8080 &
UI_PF_PID=$!
sleep 3
FRONTEND_HOST="http://localhost:8081"
echo "    UI port-forward PID: ${UI_PF_PID}, host: ${FRONTEND_HOST}"

# ── Step 6: Output dir + Prometheus port-forward ─────────────────────────────
echo "==> Step 6: Output directory and Prometheus port-forward"
mkdir -p "${OUT_DIR}"
lsof -ti:9091 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9091:9090 &
PROM_PF_PID=$!
sleep 3
echo "    Prometheus port-forward PID: ${PROM_PF_PID}"

# ── Step 7: Record start time ────────────────────────────────────────────────
echo "==> Step 7: Recording start time"
START_TIME=$(date +%s)
echo "    START_TIME=${START_TIME}"

# ── Step 8: Run Locust load test (parameterised) ────────────────────────────
LOCUSTFILE_PATH="${LOCUSTFILE:-${SCRIPT_DIR}/locustfile.py}"
LOCUST_RUN_TIME_VAL="${LOCUST_RUN_TIME:-10m}"
echo "==> Step 8: Locust (file=${LOCUSTFILE_PATH}, run_time=${LOCUST_RUN_TIME_VAL}, seed=${LOCUST_SEED:-<unset>})"
[[ -n "${LOCUST_SEED:-}" ]] && export LOCUST_SEED
locust -f "${LOCUSTFILE_PATH}" --headless \
    --host "${FRONTEND_HOST}" \
    --run-time "${LOCUST_RUN_TIME_VAL}" \
    --csv "${OUT_DIR}/locust" --csv-full-history \
    --loglevel WARNING || true

# ── Step 9: Record end time ──────────────────────────────────────────────────
echo "==> Step 9: Recording end time"
END_TIME=$(date +%s)
echo "    END_TIME=${END_TIME}"

# ── Step 10: Restart Prometheus port-forward (collect_metrics needs it) ─────
echo "==> Step 10: Restarting Prometheus port-forward"
kill "${PROM_PF_PID}" 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9091:9090 &
PROM_PF_PID=$!
sleep 3

# ── Step 11: Collect metrics ─────────────────────────────────────────────────
echo "==> Step 11: Collecting metrics from Prometheus"
python3 "${SCRIPT_DIR}/collect_metrics.py" \
    --start "${START_TIME}" \
    --end   "${END_TIME}" \
    --namespace "${APP_NAMESPACE}" \
    --out "${OUT_DIR}"

# ── Step 12: HPA snapshot for the record ────────────────────────────────────
echo "==> Step 12: Snapshotting HPA state"
kubectl get hpa -n "${APP_NAMESPACE}" -o yaml > "${OUT_DIR}/hpa_state.yaml" 2>/dev/null || true

# ── Step 13: Cleanup ─────────────────────────────────────────────────────────
echo "==> Step 13: Cleanup HPAs and port-forwards"
kubectl delete hpa --all -n "${APP_NAMESPACE}" 2>/dev/null || true
kill "${PROM_PF_PID}" "${UI_PF_PID}" 2>/dev/null || true

echo ""
echo "==> KHPA Train Ticket experiment complete."
echo "    Duration: $((END_TIME - START_TIME))s"
echo "    Output:   ${OUT_DIR}"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
