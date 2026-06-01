#!/usr/bin/env bash
# Run PBScaler against the Train Ticket benchmark on DOKS.
#
# Same shape as run_pbscaler_train_ticket.sh (GCP) but:
#   - Sources scripts/do.env (DOKS cluster, no GCP project)
#   - Fetches kubeconfig via `doctl kubernetes cluster kubeconfig save`
#   - Same config.yaml swap (TT model + namespace), same trap-restore
#
# Same env vars are honoured:
#   LOCUSTFILE      path to a locustfile (default: scripts/locustfile.py — wrong for TT)
#   LOCUST_RUN_TIME duration string (default: 10m)
#   LOCUST_SEED     integer seed exported for the locustfile
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/do.env
source "${SCRIPT_DIR}/do.env"

OUT_DIR="${PROJECT_ROOT}/results/pbscaler_train_ticket"
CONFIG_PATH="${PROJECT_ROOT}/config.yaml"
CONFIG_BACKUP="${PROJECT_ROOT}/config.yaml.bak"
TT_MODEL_REL="simulation/train_ticket/RandomForestClassify.model"

# ── Step 0: Prerequisites ────────────────────────────────────────────────────
echo "==> Step 0: Checking prerequisites"
for cmd in kubectl locust python3 doctl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found in PATH"
        exit 1
    fi
done
echo "    kubectl, locust, python3, doctl — OK"

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

python3 - "$CONFIG_PATH" "$APP_NAMESPACE" "$TT_MODEL_REL" <<'PY'
import sys, yaml, pathlib
path, namespace, model_rel = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(pathlib.Path(path).read_text())
cfg.setdefault('kubernetes', {})['namespace'] = namespace
cfg.setdefault('autoscaler', {})['simulation_model'] = model_rel
pathlib.Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
echo "    config.yaml now targets namespace=${APP_NAMESPACE} model=${TT_MODEL_REL}"

# ── Step 3: DOKS credentials ─────────────────────────────────────────────────
echo "==> Step 3: Fetching DOKS credentials"
doctl kubernetes cluster kubeconfig save "${CLUSTER_NAME}"

# ── Step 4: Delete any existing HPAs ─────────────────────────────────────────
echo "==> Step 4: Deleting existing HPAs (PBScaler manages scaling itself)"
kubectl delete hpa --all -n "${APP_NAMESPACE}" 2>/dev/null || true

# ── Step 5: Reset all deployments to 1 replica (skippable via SKIP_RESET=1) ─
# When SKIP_RESET=1 is set by the caller (e.g. run_one_tt_run.sh's pre-flight),
# this step is omitted to avoid a kubectl stampede that saturates the DOKS API
# server while measure_phantom_capacity.py is sampling. See
# results/sprint-1/train-ticket/step/run99/CALIBRATION_NOTES.md for the bug
# diagnosis.
if [[ "${SKIP_RESET:-0}" != "1" ]]; then
    echo "==> Step 5: Resetting all deployments to 1 replica"
    for deploy in $(kubectl get deployments -n "${APP_NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
        kubectl scale deployment "${deploy}" --replicas=1 -n "${APP_NAMESPACE}"
    done
    echo "    Waiting 60s for Java pods to stabilise..."
    sleep 60
else
    echo "==> Step 5: reset skipped (SKIP_RESET=1) — caller handled it pre-flight"
fi

# ── Step 6: Locate UI dashboard endpoint via port-forward ────────────────────
echo "==> Step 6: Locating Train Ticket frontend"
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

# ── Step 7: Output dir + Prometheus port-forward ─────────────────────────────
echo "==> Step 7: Creating output directory and Prometheus port-forward"
mkdir -p "${OUT_DIR}"
lsof -ti:9091 | xargs kill 2>/dev/null || true
sleep 1
kubectl port-forward -n "${MON_NAMESPACE}" \
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9091:9090 &
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
    "svc/${PROM_RELEASE}-kube-prom-prometheus" 9091:9090 &
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
fi

# ── Step 14: Cleanup ─────────────────────────────────────────────────────────
echo "==> Step 14: Cleanup"
kill "${PROM_PF_PID}" "${UI_PF_PID}" 2>/dev/null || true

echo ""
echo "==> Train Ticket experiment complete."
echo "    Duration: $((END_TIME - START_TIME))s  (${START_TIME} -> ${END_TIME})"
echo "    Output:   ${OUT_DIR}"
ls -1 "${OUT_DIR}" | sed 's/^/      /'
