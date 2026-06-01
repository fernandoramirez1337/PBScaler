#!/usr/bin/env bash
# Kubernetes HPA setup for Online Boutique (namespace=online-boutique).
# Sprint 1.5 — KHPA baseline against PBScaler vanilla.
# Parameters chosen for parity with PBScaler config: max=5 (config.yaml max_pod=5),
# CPU target 80% (Kubernetes default for cpu-based HPAs).

set -e
NS=online-boutique

kubectl autoscale deployment frontend              -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment cartservice           -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment adservice             -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment checkoutservice       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment emailservice          -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment productcatalogservice -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment recommendationservice -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment paymentservice        -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment shippingservice       -n "$NS" --min=1 --max=5 --cpu-percent=80
kubectl autoscale deployment currencyservice       -n "$NS" --min=1 --max=5 --cpu-percent=80
