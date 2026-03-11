#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# teardown_gke.sh — Delete the PBScaler experiment GKE cluster
#
# Removes (in order):
#   1. Helm releases (kube-prometheus-stack)
#   2. App and monitoring namespaces
#   3. Istio (istioctl uninstall --purge)
#   4. The GKE cluster itself
#
# Idempotent: safe to re-run — each step checks before deleting.
#
# Usage:
#   bash scripts/teardown_gke.sh
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/gke.env"

# ── Colours ───────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
banner(){ echo -e "\n${CYAN}════════════════════════════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}════════════════════════════════════════════════════════════════${NC}\n"; }

# ── Validate ──────────────────────────────────────────────────────────
if [[ -z "$PROJECT_ID" ]]; then
    err "PROJECT_ID is empty.  Edit scripts/gke.env and set your GCP project ID."
    exit 1
fi

banner "Teardown — $CLUSTER_NAME ($ZONE)"
echo -e "${YELLOW}This will permanently delete the cluster and all workloads.${NC}"
echo ""
read -rp "Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    info "Aborted."
    exit 0
fi

# ── Check if cluster exists and get credentials ──────────────────────
if ! gcloud container clusters describe "$CLUSTER_NAME" --zone "$ZONE" --project "$PROJECT_ID" &>/dev/null; then
    warn "Cluster '$CLUSTER_NAME' does not exist in $ZONE — nothing to delete."
    exit 0
fi

info "Getting cluster credentials…"
gcloud container clusters get-credentials "$CLUSTER_NAME" \
    --zone "$ZONE" \
    --project "$PROJECT_ID" \
    --quiet 2>/dev/null || true

# ── 1. Delete Helm releases ──────────────────────────────────────────
banner "Step 1 — Helm Releases"

if helm status "$PROM_RELEASE" -n "$MON_NAMESPACE" &>/dev/null; then
    info "Uninstalling Helm release '$PROM_RELEASE'…"
    helm uninstall "$PROM_RELEASE" -n "$MON_NAMESPACE" --wait || warn "Helm uninstall failed (non-fatal)"
    ok "Helm release '$PROM_RELEASE' removed"
else
    ok "Helm release '$PROM_RELEASE' not found — skipping"
fi

# Clean up CRDs left by kube-prometheus-stack
info "Cleaning up Prometheus CRDs…"
kubectl delete crd prometheuses.monitoring.coreos.com \
    prometheusrules.monitoring.coreos.com \
    servicemonitors.monitoring.coreos.com \
    podmonitors.monitoring.coreos.com \
    alertmanagers.monitoring.coreos.com \
    alertmanagerconfigs.monitoring.coreos.com \
    probes.monitoring.coreos.com \
    thanosrulers.monitoring.coreos.com \
    scrapeconfigs.monitoring.coreos.com \
    prometheusagents.monitoring.coreos.com \
    2>/dev/null || ok "CRDs already removed or not found"

# ── 2. Delete namespaces ──────────────────────────────────────────────
banner "Step 2 — Namespaces"

for ns in "$APP_NAMESPACE" "$MON_NAMESPACE"; do
    if kubectl get namespace "$ns" &>/dev/null; then
        info "Deleting namespace '$ns'…"
        kubectl delete namespace "$ns" --timeout=120s || warn "Namespace '$ns' deletion timed out"
        ok "Namespace '$ns' deleted"
    else
        ok "Namespace '$ns' does not exist — skipping"
    fi
done

# ── 3. Uninstall Istio ────────────────────────────────────────────────
banner "Step 3 — Istio"

if kubectl get namespace istio-system &>/dev/null; then
    info "Uninstalling Istio…"
    if command -v istioctl &>/dev/null; then
        istioctl uninstall --purge -y 2>/dev/null || warn "istioctl uninstall had warnings"
    else
        warn "istioctl not found — deleting istio-system namespace directly"
    fi
    kubectl delete namespace istio-system --timeout=120s 2>/dev/null || true
    ok "Istio removed"
else
    ok "Istio not installed (istio-system not found) — skipping"
fi

# ── 4. Delete GKE cluster ────────────────────────────────────────────
banner "Step 4 — GKE Cluster"

info "Deleting cluster '$CLUSTER_NAME' in $ZONE…"
gcloud container clusters delete "$CLUSTER_NAME" \
    --zone "$ZONE" \
    --project "$PROJECT_ID" \
    --quiet

ok "Cluster '$CLUSTER_NAME' deleted"

# ── Done ──────────────────────────────────────────────────────────────
banner "Teardown Complete"
echo -e "  ${GREEN}All resources deleted.  No ongoing billing for this cluster.${NC}"
echo ""
