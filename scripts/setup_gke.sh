#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# setup_gke.sh -- Provision a minimal GKE experiment cluster for PBScaler
#
# Creates:
#   • 3-node e2-standard-4 GKE cluster
#   • Istio 1.13.4 service mesh (manual install via istioctl)
#   • Online Boutique micro­services in namespace "online-boutique"
#   • kube-prometheus-stack (Prometheus + Grafana) in namespace "monitoring"
#   • Prometheus scrape config for Istio/Envoy metrics
#
# Idempotent: safe to re-run -- each step checks before creating.
#
# Usage:
#   1. Edit scripts/gke.env  →  set PROJECT_ID
#   2. bash scripts/setup_gke.sh
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/gke.env"

# Defaults if gke.env didn't define these
BOUTIQUE_MANIFESTS="${BOUTIQUE_MANIFESTS:-benchmarks/microservices-demo/kubernetes-manifests}"
ISTIO_MANIFESTS="${ISTIO_MANIFESTS:-benchmarks/microservices-demo/istio-manifests}"

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
    echo "  Install them before running this script."
    echo "    gcloud:   https://cloud.google.com/sdk/docs/install"
    echo "    kubectl:  gcloud components install kubectl"
    echo "    helm:     https://helm.sh/docs/intro/install/"
    echo "    istioctl: curl -L https://istio.io/downloadIstio | ISTIO_VERSION=$ISTIO_VERSION sh -"
    exit 1
fi
ok "All tools found: gcloud, kubectl, helm, istioctl"

if [[ -z "$PROJECT_ID" ]]; then
    err "PROJECT_ID is empty.  Edit scripts/gke.env and set your GCP project ID."
    exit 1
fi
ok "PROJECT_ID = $PROJECT_ID"

# Set gcloud project
gcloud config set project "$PROJECT_ID" --quiet
ok "gcloud project set to $PROJECT_ID"

# ── 1. Create GKE cluster ────────────────────────────────────────────
banner "Step 1 -- GKE Cluster"

if gcloud container clusters describe "$CLUSTER_NAME" --zone "$ZONE" --project "$PROJECT_ID" &>/dev/null; then
    ok "Cluster '$CLUSTER_NAME' already exists in $ZONE -- skipping creation"
else
    info "Creating cluster '$CLUSTER_NAME' (3 × ${MACHINE_TYPE} in ${ZONE})..."
    gcloud container clusters create "$CLUSTER_NAME" \
        --zone "$ZONE" \
        --machine-type "$MACHINE_TYPE" \
        --num-nodes "$NUM_NODES" \
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
    ok "Istio already installed (istiod found in istio-system) -- skipping"
else
    info "Installing Istio $ISTIO_VERSION with default profile..."
    istioctl install --set profile=default -y
    ok "Istio installed"
fi

# Wait for istiod to be ready
info "Waiting for istiod to be ready..."
kubectl rollout status deployment/istiod -n istio-system --timeout=120s
ok "istiod is ready"

# ── 4. Create app namespace with sidecar injection ────────────────────
banner "Step 4 -- App Namespace ($APP_NAMESPACE)"

if kubectl get namespace "$APP_NAMESPACE" &>/dev/null; then
    ok "Namespace '$APP_NAMESPACE' already exists"
else
    kubectl create namespace "$APP_NAMESPACE"
    ok "Namespace '$APP_NAMESPACE' created"
fi

# Ensure sidecar injection is enabled
kubectl label namespace "$APP_NAMESPACE" istio-injection=enabled --overwrite
ok "Istio sidecar injection enabled for $APP_NAMESPACE"

# ── 5. Deploy Online Boutique ────────────────────────────────────────
banner "Step 5 -- Online Boutique"

info "Applying Kubernetes manifests from ${BOUTIQUE_MANIFESTS}..."
kubectl apply -f "$PROJECT_ROOT/$BOUTIQUE_MANIFESTS/" -n "$APP_NAMESPACE"
ok "Online Boutique workloads applied"

# Apply Istio manifests (patch namespace from "hipster" → APP_NAMESPACE)
info "Applying Istio manifests (gateway, virtual service, egress)..."
for f in "$PROJECT_ROOT/$ISTIO_MANIFESTS/"*.yaml; do
    sed "s/namespace: hipster/namespace: $APP_NAMESPACE/g" "$f" \
        | kubectl apply -n "$APP_NAMESPACE" -f -
done
ok "Istio manifests applied"

# ── 6. Install kube-prometheus-stack ──────────────────────────────────
banner "Step 6 -- kube-prometheus-stack"

# Add Helm repo (idempotent)
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

kubectl apply -f - <<'EOF'
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
      - online-boutique
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

# ── 8. Wait for Online Boutique pods ──────────────────────────────────
banner "Step 8 -- Verifying Online Boutique pods"

info "Waiting for all deployments in $APP_NAMESPACE to be ready (timeout 5m)..."

deployments=$(kubectl get deployments -n "$APP_NAMESPACE" -o jsonpath='{.items[*].metadata.name}')
for dep in $deployments; do
    info "  Waiting for ${dep}..."
    kubectl rollout status deployment/"$dep" -n "$APP_NAMESPACE" --timeout=300s || {
        warn "$dep did not become ready in time"
    }
done

echo ""
info "Pod status in $APP_NAMESPACE:"
kubectl get pods -n "$APP_NAMESPACE" -o wide
echo ""

# Check sidecar injection (expect 2/2 or 3/3 READY containers)
info "Checking sidecar injection..."
NOT_INJECTED=0
while IFS= read -r line; do
    pod_name=$(echo "$line" | awk '{print $1}')
    ready=$(echo "$line" | awk '{print $2}')
    containers_ready=$(echo "$ready" | cut -d/ -f1)
    containers_total=$(echo "$ready" | cut -d/ -f2)
    if [[ "$containers_total" -lt 2 ]]; then
        warn "  $pod_name has $containers_total container(s) -- sidecar may not be injected"
        NOT_INJECTED=$((NOT_INJECTED + 1))
    else
        ok "  $pod_name -- $ready containers (sidecar present)"
    fi
done < <(kubectl get pods -n "$APP_NAMESPACE" --no-headers 2>/dev/null | grep -v "Completed\|Terminating")

if [[ $NOT_INJECTED -gt 0 ]]; then
    warn "$NOT_INJECTED pod(s) may be missing Istio sidecars"
else
    ok "All pods have Istio sidecars injected"
fi

# ── 9. Verify Prometheus ──────────────────────────────────────────────
banner "Step 9 -- Verifying Prometheus"

info "Prometheus pods in $MON_NAMESPACE:"
kubectl get pods -n "$MON_NAMESPACE" -l app.kubernetes.io/name=prometheus
echo ""

info "Grafana pods in $MON_NAMESPACE:"
kubectl get pods -n "$MON_NAMESPACE" -l app.kubernetes.io/name=grafana
echo ""

# ── 10. Port-forward commands ─────────────────────────────────────────
banner "Step 10 -- Port-Forward Commands"

GRAFANA_PASS=$(kubectl get secret -n "$MON_NAMESPACE" "${PROM_RELEASE}-grafana" \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || echo "prom-operator")

cat <<CMDS

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Port-forward commands (run in separate terminals)                 │
  ├─────────────────────────────────────────────────────────────────────┤
  │                                                                     │
  │  Prometheus  (http://localhost:9090):                               │
  │    kubectl port-forward -n $MON_NAMESPACE svc/${PROM_RELEASE}-kube-prom-prometheus 9090:9090
  │                                                                     │
  │  Grafana  (http://localhost:3000):                                  │
  │    kubectl port-forward -n $MON_NAMESPACE svc/${PROM_RELEASE}-grafana 3000:80
  │    Login: admin / ${GRAFANA_PASS}
  │                                                                     │
  │  Online Boutique  (http://localhost:8080):                          │
  │    kubectl port-forward -n $APP_NAMESPACE svc/frontend 8080:80
  │                                                                     │
  │  PBScaler config.yaml overrides:                                   │
  │    export PROM_RANGE_URL=http://localhost:9090/api/v1/query_range   │
  │    export PROM_QUERY_URL=http://localhost:9090/api/v1/query         │
  │    export K8S_NAMESPACE=$APP_NAMESPACE                              │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

CMDS

ok "Setup complete!  Cluster: $CLUSTER_NAME ($ZONE), $NUM_NODES × $MACHINE_TYPE"
warn "Remember to run  scripts/teardown_gke.sh  when done to avoid billing."
