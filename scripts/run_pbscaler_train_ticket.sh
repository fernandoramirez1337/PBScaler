#!/usr/bin/env bash
# Run PBScaler against the Train Ticket benchmark.
#
# Same structure as run_pbscaler_baseline.sh (Online Boutique), with two
# Train-Ticket-specific differences:
#
#   1. Sources scripts/tt.env (different cluster, namespace).
#   2. Temporarily swaps config.yaml to point at simulation/train_ticket/rf.pkl,
#      since Config.py has no env override for the model path. The original
#      config.yaml is backed up to config.yaml.bak and restored on exit.
#
# Same env vars are honoured by Step 10 (Locust):
#   LOCUSTFILE      path to a locustfile (default: scripts/locustfile.py — wrong for TT)
#   LOCUST_RUN_TIME duration string (default: 10m)
#   LOCUST_SEED     integer seed exported for the locustfile
#
# Usage example (from Fase G loop):
#   LOCUSTFILE=../../benchmarks/train_ticket/locustfile_step.py \
#   LOCUST_RUN_TIME=1800s \
#   LOCUST_SEED=142 \
#       bash scripts/run_pbscaler_train_ticket.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/tt.env
source "${SCRIPT_DIR}/tt.env"

OUT_DIR="${PROJECT_ROOT}/results/pbscaler_train_ticket"
CONFIG_PATH="${PROJECT_ROOT}/config.yaml"
CONFIG_BACKUP="${PROJECT_ROOT}/config.yaml.bak"
TT_MODEL_REL="simulation/train_ticket/RandomForestClassify.model"

# ── Step 0: Prerequisites ────────────────────────────────────────────────────
echo "==> Step 0: Checking prerequisites"
for cmd in kubectl locust python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        exit 1
    fi
done
echo "    kubectl, locust, python3 — OK"

pip3 install --quiet schedule pyyaml

# ── Step 1: Restore config.yaml on exit (always) ─────────────────────────────
restore_config() {
    if [[ -f "${CONFIG_BACKUP}" ]]; then
        mv -f "${CONFIG_BACKUP}" "${CONFIG_PATH}"
        echo "    config.yaml restored from backup"
    fi
}
trap restore_config EXIT

# ── Step 2: Swap config.yaml to point at TT model + namespace ────────────────
echo "==> Step 2: Swapping config.yaml for Train Ticket"
if [[ -f "${CONFIG_BACKUP}" ]]; then
    echo "ERROR: ${CONFIG_BACKUP} already exists — refusing to overwrite. "
    echo "       Did a previous run die mid-flight? Inspect both files and rm/move the .bak manually."
    exit 1
fi
cp "${CONFIG_PATH}" "${CONFIG_BACKUP}"

# Use a short Python heredoc to safely rewrite YAML keys without sed-escaping.
python3 - "$CONFIG_PATH" "$APP_NAMESPACE" "$TT_MODEL_REL" <<'PY'
import sys, yaml, pathlib
path, namespace, model_rel = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(pathlib.Path(path).read_text())
cfg.setdefault('kubernetes', {})['namespace'] = namespace
cfg.setdefault('autoscaler', {})['simulation_model'] = model_rel
pathlib.Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
echo "    config.yaml now targets namespace=${APP_NAMESPACE} model=${TT_MODEL_REL}"

# ── Step 3: GKE credentials ──────────────────────────────────────────────────
echo "==> Step 3: Fetching GKE credentials"
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
    --zone "${ZONE}" --project "${PROJECT_ID}"

# ── Step 4: Delete any existing HPAs ─────────────────────────────────────────
echo "==> Step 4: Deleting existing HPAs (PBScaler manages scaling itself)"
kubectl delete hpa --all -n "${APP_NAMESPACE}" 2>/dev/null || true

# ── Step 5: Reset all deployments to 1 replica ──────────────────────────────
echo "==> Step 5: Resetting all deployments to 1 replica"
for deploy in $(kubectl get deployments -n "${APP_NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    kubectl scale deployment "${deploy}" --replicas=1 -n "${APP_NAMESPACE}"
done
echo "    Waiting 60s for Java pods to stabilise..."
sleep 60

# ── Step 6: Locate UI dashboard endpoint via NodePort or port-forward ────────
echo "==> Step 6: Locating Train Ticket frontend"
UI_SVC="ts-ui-dashboard"
if ! kubectl get svc "${UI_SVC}" -n "${APP_NAMESPACE}" &>/dev/null; then
    echo "ERROR: service ${UI_SVC} not found in namespace ${APP_NAMESPACE}"
    exit 1
fi

# Train Ticket exposes ts-ui-dashboard as NodePort (no LoadBalancer by default).
# Use port-forward to a stable localhost address that Locust can hit.
lsof -ti:8080 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${APP_NAMESPACE}" "svc/${UI_SVC}" 8080:8080 &
UI_PF_PID=$!
sleep 3
FRONTEND_HOST="http://localhost:8080"
echo "    UI port-forward PID: ${UI_PF_PID}, host: ${FRONTEND_HOST}"

# ── Step 7: Output dir + Prometheus port-forward ─────────────────────────────
echo "==> Step 7: Creating output directory and Prometheus port-forward"
mkdir -p "${OUT_DIR}"
lsof -ti:9090 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PROM_PF_PID=$!
sleep 3
echo "    Prometheus port-forward PID: ${PROM_PF_PID}"

# ── Step 8: Record start time + start PBScaler ───────────────────────────────
echo "==> Step 8: Starting PBScaler controller"
START_TIME=$(date +%s)
echo "    START_TIME=${START_TIME}"
cd "${PROJECT_ROOT}"
K8S_NAMESPACE="${APP_NAMESPACE}" python3 main.py 2>&1 | tee "${OUT_DIR}/pbscaler.log" &
PBSCALER_PID=$!
echo "    PBScaler PID: ${PBSCALER_PID}"
sleep 5
if ! kill -0 "${PBSCALER_PID}" 2>/dev/null; then
    echo "ERROR: PBScaler crashed on startup. Check ${OUT_DIR}/pbscaler.log"
    tail -30 "${OUT_DIR}/pbscaler.log" 2>/dev/null || true
    kill "${PROM_PF_PID}" "${UI_PF_PID}" 2>/dev/null || true
    exit 1
fi
echo "    PBScaler health check passed — still running after 5s"

# ── Step 9: Run Locust load test (parameterised) ────────────────────────────
LOCUSTFILE_PATH="${LOCUSTFILE:-${SCRIPT_DIR}/locustfile.py}"
LOCUST_RUN_TIME_VAL="${LOCUST_RUN_TIME:-10m}"
echo "==> Step 9: Locust (file=${LOCUSTFILE_PATH}, run_time=${LOCUST_RUN_TIME_VAL}, seed=${LOCUST_SEED:-<unset>})"
[[ -n "${LOCUST_SEED:-}" ]] && export LOCUST_SEED
locust -f "${LOCUSTFILE_PATH}" --headless \
    --host "${FRONTEND_HOST}" \
    --run-time "${LOCUST_RUN_TIME_VAL}" \
    --csv "${OUT_DIR}/locust" --csv-full-history \
    --loglevel WARNING || true

# ── Step 10: Stop PBScaler ───────────────────────────────────────────────────
echo "==> Step 10: Stopping PBScaler"
END_TIME=$(date +%s)
echo "    END_TIME=${END_TIME}"
kill "${PBSCALER_PID}" 2>/dev/null || true
wait "${PBSCALER_PID}" 2>/dev/null || true
echo "    PBScaler stopped"

# ── Step 11: Restart Prometheus port-forward (collect_metrics needs it) ──────
echo "==> Step 11: Restarting Prometheus port-forward"
kill "${PROM_PF_PID}" 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9090:9090 &
PROM_PF_PID=$!
sleep 3

# ── Step 12: Collect metrics ─────────────────────────────────────────────────
echo "==> Step 12: Collecting metrics from Prometheus"
python3 "${SCRIPT_DIR}/collect_metrics.py" \
    --start "${START_TIME}" \
    --end   "${END_TIME}" \
    --namespace "${APP_NAMESPACE}" \
    --out "${OUT_DIR}"

# ── Step 13: Print key log lines ─────────────────────────────────────────────
echo "==> Step 13: PBScaler log summary"
if [[ -f "${OUT_DIR}/pbscaler.log" ]]; then
    echo "    Anomaly detections:"
    grep -c 'ANOMALY:.*abnormal calls out of' "${OUT_DIR}/pbscaler.log" 2>/dev/null | xargs -I{} echo "      {} anomaly checks logged"
    echo "    Scaling events:"
    grep 'SCALE:' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -20 | sed 's/^/      /'
    echo "    GA optimizations:"
    grep 'GA_OPT: GA result' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -10 | sed 's/^/      /'
    echo "    PageRank analyses:"
    grep 'PAGERANK: Selected roots' "${OUT_DIR}/pbscaler.log" 2>/dev/null | tail -10 | sed 's/^/      /'
fi

# ── Step 14: Cleanup ─────────────────────────────────────────────────────────
echo "==> Step 14: Cleanup"
kill "${PROM_PF_PID}" "${UI_PF_PID}" 2>/dev/null || true

echo ""
echo "==> Train Ticket experiment complete."
echo "    Duration: $((END_TIME - START_TIME))s  (${START_TIME} -> ${END_TIME})"
echo "    Output:   ${OUT_DIR}"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
