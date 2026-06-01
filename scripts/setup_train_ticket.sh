#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# setup_train_ticket.sh -- Provision a GKE cluster for Train Ticket.
#
# Creates:
#   • 4-node n2-standard-4 GKE cluster (16 GB RAM × 4 = 64 GB total)
#   • Istio service mesh
#   • Train Ticket micro­services in namespace "train-ticket"
#     deployed in 3 phases (databases → services → UI) per the upstream layout
#   • kube-prometheus-stack in "monitoring"
#
# Idempotent: safe to re-run.
#
# Usage:
#   1. Edit scripts/tt.env  →  set PROJECT_ID
#   2. bash scripts/setup_train_ticket.sh
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/tt.env"

TT_MANIFESTS="${TT_MANIFESTS:-benchmarks/train-ticket/deployment/kubernetes-manifests/quickstart-k8s}"

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
banner(){ echo -e "\n${CYAN}════════════════════════════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}════════════════════════════════════════════════════════════════${NC}\n"; }

# ── 0. Prerequisites ─────────────────────────────────────────────────
banner "Step 0 -- Checking prerequisites"

missing=()
for cmd in gcloud kubectl helm istioctl; do
    if ! command -v "$cmd" &>/dev/null; then missing+=("$cmd"); fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
    err "Missing required tools: ${missing[*]}"
    exit 1
fi
ok "All tools found"

if [[ -z "$PROJECT_ID" ]]; then
    err "PROJECT_ID is empty. Edit scripts/tt.env and set your GCP project ID."
    exit 1
fi
ok "PROJECT_ID = $PROJECT_ID"

gcloud config set project "$PROJECT_ID" --quiet
ok "gcloud project set to $PROJECT_ID"

# ── 1. Create GKE cluster ────────────────────────────────────────────
banner "Step 1 -- GKE Cluster (Train Ticket sizing)"

if gcloud container clusters describe "$CLUSTER_NAME" --zone "$ZONE" --project "$PROJECT_ID" &>/dev/null; then
    ok "Cluster '$CLUSTER_NAME' already exists in $ZONE -- skipping creation"
else
    info "Creating cluster '$CLUSTER_NAME' (${NUM_NODES} × ${MACHINE_TYPE} in ${ZONE})..."
    # --disk-size 50: defaults to 100 GB pd-balanced which exceeds the
    # default SSD_TOTAL_GB=250 quota at 3 nodes. 50 GB × 3 = 150 GB fits,
    # and is more than enough for the Train Ticket smoke deploy.
    gcloud container clusters create "$CLUSTER_NAME" \
        --zone "$ZONE" \
        --machine-type "$MACHINE_TYPE" \
        --num-nodes "$NUM_NODES" \
        --disk-size 50 \
        --enable-ip-alias \
        --no-enable-autoupgrade \
        --no-enable-autorepair \
        --project "$PROJECT_ID" \
        --quiet
    ok "Cluster '$CLUSTER_NAME' created"
fi

# ── 2. Get credentials ───────────────────────────────────────────────
banner "Step 2 -- Cluster Credentials"

gcloud container clusters get-credentials "$CLUSTER_NAME" \
    --zone "$ZONE" \
    --project "$PROJECT_ID" \
    --quiet
ok "kubectl context set to $CLUSTER_NAME"

# ── 3. Install Istio ─────────────────────────────────────────────────
banner "Step 3 -- Istio $ISTIO_VERSION"

if kubectl get deployment istiod -n istio-system &>/dev/null; then
    ok "Istio already installed -- skipping"
else
    info "Installing Istio with default profile..."
    istioctl install --set profile=default -y
    ok "Istio installed"
fi

info "Waiting for istiod to be ready..."
kubectl rollout status deployment/istiod -n istio-system --timeout=180s
ok "istiod is ready"

# ── 4. Create app namespace with sidecar injection ────────────────────
banner "Step 4 -- App Namespace ($APP_NAMESPACE)"

if kubectl get namespace "$APP_NAMESPACE" &>/dev/null; then
    ok "Namespace '$APP_NAMESPACE' already exists"
else
    kubectl create namespace "$APP_NAMESPACE"
    ok "Namespace '$APP_NAMESPACE' created"
fi

kubectl label namespace "$APP_NAMESPACE" istio-injection=enabled --overwrite
ok "Istio sidecar injection enabled for $APP_NAMESPACE"

# ── 5. Deploy Train Ticket (3 phases) ────────────────────────────────
banner "Step 5 -- Train Ticket Deployment"

MANIFEST_DIR="$PROJECT_ROOT/$TT_MANIFESTS"
if [[ ! -d "$MANIFEST_DIR" ]]; then
    err "Manifest dir not found: $MANIFEST_DIR"
    exit 1
fi

PART1="$MANIFEST_DIR/quickstart-ts-deployment-part1.yml"
PART2="$MANIFEST_DIR/quickstart-ts-deployment-part2.yml"
PART3="$MANIFEST_DIR/quickstart-ts-deployment-part3.yml"

for part in "$PART1" "$PART2" "$PART3"; do
    if [[ ! -f "$part" ]]; then
        err "Missing manifest: $part"
        exit 1
    fi
done

info "Phase 1/3 — Databases (part1.yml)"
kubectl apply -f "$PART1" -n "$APP_NAMESPACE"
info "Waiting 60 s for databases to start receiving traffic..."
sleep 60

info "Phase 2/3 — Services (part2.yml)"
kubectl apply -f "$PART2" -n "$APP_NAMESPACE"
info "Waiting 60 s for service deployments to register..."
sleep 60

info "Phase 3/3 — UI / Frontend (part3.yml)"
kubectl apply -f "$PART3" -n "$APP_NAMESPACE"
ok "All Train Ticket manifests applied"

# ── 6. Install kube-prometheus-stack ──────────────────────────────────
banner "Step 6 -- kube-prometheus-stack"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update

if kubectl get namespace "$MON_NAMESPACE" &>/dev/null; then
    ok "Namespace '$MON_NAMESPACE' already exists"
else
    kubectl create namespace "$MON_NAMESPACE"
    ok "Namespace '$MON_NAMESPACE' created"
fi

if helm status "$PROM_RELEASE" -n "$MON_NAMESPACE" &>/dev/null; then
    ok "Helm release '$PROM_RELEASE' already installed -- upgrading..."
    HELM_CMD="upgrade"
else
    HELM_CMD="install"
fi

helm $HELM_CMD "$PROM_RELEASE" prometheus-community/kube-prometheus-stack \
    -n "$MON_NAMESPACE" \
    --wait --timeout 5m0s \
    -f /dev/stdin <<'VALUES'
prometheus:
  prometheusSpec:
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    additionalScrapeConfigs:
      - job_name: "istio-envoy"
        metrics_path: /stats/prometheus
        kubernetes_sd_configs:
          - role: pod
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_container_name]
            action: keep
            regex: istio-proxy
          - source_labels: [__address__]
            action: replace
            regex: '([^:]+)(:\d+)?'
            replacement: '$1:15020'
            target_label: __address__
          - source_labels: [__meta_kubernetes_namespace]
            target_label: namespace
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod_name
          - source_labels: [__meta_kubernetes_pod_label_app]
            target_label: destination_workload
VALUES

ok "kube-prometheus-stack $HELM_CMD'd"

# ── 7. Istio Prometheus integration -- PodMonitor ─────────────────────
banner "Step 7 -- Istio metrics PodMonitor"

kubectl apply -f - <<EOF
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: istio-envoy-stats
  namespace: monitoring
  labels:
    app: istio-proxy
spec:
  namespaceSelector:
    matchNames:
      - $APP_NAMESPACE
      - istio-system
  selector:
    matchExpressions:
      - key: istio.io/rev
        operator: Exists
  podMetricsEndpoints:
    - port: http-envoy-prom
      path: /stats/prometheus
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_container_name]
          action: keep
          regex: istio-proxy
        - sourceLabels: [__meta_kubernetes_pod_label_app]
          targetLabel: destination_workload
EOF
ok "PodMonitor for Istio Envoy sidecars created"

# ── 8. Wait for Train Ticket pods (longer timeout — 41 Java services) ─
banner "Step 8 -- Verifying Train Ticket pods"

info "Waiting up to 10 min for all deployments to be ready..."

deployments=$(kubectl get deployments -n "$APP_NAMESPACE" -o jsonpath='{.items[*].metadata.name}')
total=$(echo "$deployments" | wc -w | tr -d ' ')
info "Total deployments: $total"

failed=()
for dep in $deployments; do
    info "  Waiting for ${dep}..."
    if ! kubectl rollout status deployment/"$dep" -n "$APP_NAMESPACE" --timeout=600s; then
        warn "$dep did not become ready in 10 min"
        failed+=("$dep")
    fi
done

echo ""
if [[ ${#failed[@]} -gt 0 ]]; then
    warn "${#failed[@]} deployment(s) failed to roll out: ${failed[*]}"
    warn "Likely cause: OOM. Inspect with: kubectl describe pod -n $APP_NAMESPACE <pod>"
    warn "Consider raising resources.requests.memory in the manifests, or NUM_NODES in tt.env"
else
    ok "All $total deployments are Ready"
fi

info "Pod status in $APP_NAMESPACE:"
kubectl get pods -n "$APP_NAMESPACE" -o wide
echo ""

# ── 9. Find UI dashboard service ──────────────────────────────────────
banner "Step 9 -- Locating UI Dashboard"

UI_SVC="ts-ui-dashboard"
if kubectl get svc "$UI_SVC" -n "$APP_NAMESPACE" &>/dev/null; then
    UI_TYPE=$(kubectl get svc "$UI_SVC" -n "$APP_NAMESPACE" -o jsonpath='{.spec.type}')
    info "Service '$UI_SVC' is of type: $UI_TYPE"
    if [[ "$UI_TYPE" == "NodePort" ]]; then
        NODE_PORT=$(kubectl get svc "$UI_SVC" -n "$APP_NAMESPACE" -o jsonpath='{.spec.ports[0].nodePort}')
        warn "UI exposed via NodePort $NODE_PORT — load tests will use 'kubectl port-forward'"
    fi
else
    warn "Service '$UI_SVC' not found"
fi

# ── 10. Helpful pointers ──────────────────────────────────────────────
banner "Step 10 -- Port-Forward Commands"

GRAFANA_PASS=$(kubectl get secret -n "$MON_NAMESPACE" "${PROM_RELEASE}-grafana" \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || echo "prom-operator")

cat <<CMDS

  Train Ticket cluster ready.

  Prometheus  (http://localhost:9090):
    kubectl port-forward -n $MON_NAMESPACE svc/${PROM_RELEASE}-kube-prom-prometheus 9090:9090

  Grafana  (http://localhost:3000):
    kubectl port-forward -n $MON_NAMESPACE svc/${PROM_RELEASE}-grafana 3000:80
    Login: admin / ${GRAFANA_PASS}

  Train Ticket UI:
    kubectl port-forward -n $APP_NAMESPACE svc/$UI_SVC 8080:8080

  PBScaler config overrides:
    export K8S_NAMESPACE=$APP_NAMESPACE
    export PROM_RANGE_URL=http://localhost:9090/api/v1/query_range
    export PROM_QUERY_URL=http://localhost:9090/api/v1/query

CMDS

ok "Setup complete. Cluster: $CLUSTER_NAME ($ZONE), $NUM_NODES × $MACHINE_TYPE"
warn "Remember to run scripts/teardown_train_ticket.sh when done."
