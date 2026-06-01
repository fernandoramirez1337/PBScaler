#!/usr/bin/env bash
# Kubernetes HPA setup for Train Ticket (namespace=train-ticket).
# Sprint 1.5 — KHPA baseline against PBScaler vanilla.
# 43 services matching simulation/RandomForestClassify.py SVCS_BY_BENCHMARK['train_ticket'].
# Excludes DBs (*-mongo, *-mysql), rabbitmq, ts-ui-dashboard (already in svcs list).
# Parameters: max=5 (parity with PBScaler config max_pod=5), CPU target 80%.

set -e
NS=train-ticket

kubectl autoscale deployment ts-admin-basic-info-service  -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-admin-order-service       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-admin-route-service       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-admin-travel-service      -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-admin-user-service        -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-assurance-service         -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-auth-service              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-avatar-service            -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-basic-service             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-cancel-service            -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-config-service            -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-consign-price-service     -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-consign-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-contacts-service          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-delivery-service          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-execute-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-food-map-service          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-food-service              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-inside-payment-service    -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-news-service              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-notification-service      -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-order-other-service       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-order-service             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-payment-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-preserve-other-service    -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-preserve-service          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-price-service             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-rebook-service            -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-route-plan-service        -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-route-service             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-seat-service              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-security-service          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-station-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-ticket-office-service     -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-ticketinfo-service        -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-train-service             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-travel-plan-service       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-travel-service            -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-travel2-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-ui-dashboard              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-user-service              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-verification-code-service -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment ts-voucher-service           -n "$NS" --min=1 --max=5 --cpu-percent=80
