#!/usr/bin/env bash
# Teardown the DOKS Train Ticket cluster.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/do.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
banner(){ echo -e "\n${CYAN}════════════════════════════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}════════════════════════════════════════════════════════════════${NC}\n"; }

if ! command -v doctl &>/dev/null; then
    err "doctl not found. brew install doctl && doctl auth init"
    exit 1
fi

banner "Teardown — $CLUSTER_NAME ($REGION)"
echo -e "${YELLOW}This will permanently delete the DOKS cluster and all workloads.${NC}"
echo ""
read -rp "Continue? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    info "Aborted."
    exit 0
fi

if ! doctl kubernetes cluster get "$CLUSTER_NAME" &>/dev/null; then
    warn "Cluster '$CLUSTER_NAME' does not exist — nothing to delete."
    exit 0
fi

# Best effort: clean up Helm + namespaces while cluster is reachable
info "Cleaning Helm release + namespaces (best effort)..."
helm uninstall "$PROM_RELEASE" -n "$MON_NAMESPACE" --wait 2>/dev/null \
    || warn "Helm uninstall failed (non-fatal)"
kubectl delete namespace "$APP_NAMESPACE" --timeout=180s 2>/dev/null || true
kubectl delete namespace "$MON_NAMESPACE" --timeout=180s 2>/dev/null || true
kubectl delete namespace istio-system --timeout=120s 2>/dev/null || true

banner "Deleting cluster"
# --dangerous removes the cluster + all associated load balancers + volumes
doctl kubernetes cluster delete "$CLUSTER_NAME" --dangerous --force
ok "Cluster '$CLUSTER_NAME' and associated resources deleted"

banner "Teardown Complete"
echo -e "  ${GREEN}All resources deleted. No ongoing billing for this cluster.${NC}"
echo ""
