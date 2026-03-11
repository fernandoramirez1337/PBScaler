# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PBScaler is a bottleneck-aware autoscaling framework for Kubernetes microservice applications (IEEE TSC 2024). It detects SLO violations using statistical tests, identifies bottleneck services via PageRank on the call dependency graph, and uses a Genetic Algorithm to find optimal replica counts.

**Target environments**: Kubernetes 1.20.4+ with Istio 1.13.4+ and Prometheus for metrics.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Train the SLO violation prediction model (required before running PBScaler)
cd simulation && python RandomForestClassify.py

# Run the autoscaler (edit main.py for paths first)
python main.py

# Collect post-experiment metrics to CSV
# (called programmatically from MetricCollect.collect(config, './output'))
```

There is no test suite. Validation is done by running experiments against real or simulated Kubernetes clusters.

## Configuration

All runtime parameters live in `config/Config.py`. Before running, set:

```python
self.namespace    # K8s namespace (default: 'default')
self.SLO          # Target p90 latency in ms (default: 200)
self.max_pod      # Max replicas per service (default: 8)
self.k8s_config   # Path to kubeconfig file
self.prom_range_url    # Prometheus query_range URL
self.prom_no_range_url # Prometheus instant query URL
self.duration     # Experiment duration in seconds (default: 1200)
```

**Note**: `main.py` has a hardcoded `simulation_model_path` that must point to a pre-trained `.model` file.

## Architecture

### Core Loop (`PBScaler.py`)

Two concurrent loops:

1. **Anomaly detection** (every 15s): Queries Prometheus for p90 call latencies. Flags edges where `latency > SLO * 1.1` using a one-sample t-test (CONF=0.05, ALPHA=0.2).

2. **Waste detection** (every 120s): Two-sample t-test comparing current vs. past QPS. If load dropped significantly (BETA=0.9 threshold), marks services for scale-down.

When anomalies are detected:
- Builds a weighted DAG from abnormal call edges (weights = Pearson correlation of latency time series)
- Computes **topology potential** per service (summing direct + propagated anomaly weights)
- Runs **PageRank** with topology potential as personalization vector
- Selects top-K=2 services not already at `max_pod`
- Runs **Genetic Algorithm** (`util/GA.py`) to find optimal replica vector
  - Population: 50, Generations: 5
  - Fitness: `0.5 * SLO_reward + 0.5 * cost_reward`, evaluated using the pre-trained RandomForest model
- Applies scaling via `KubernetesClient.patch_namespaced_deployment_scale()`

### Key Modules

| Module | Role |
|--------|------|
| `PBScaler.py` | Core algorithm: anomaly detection, root cause analysis, GA optimization |
| `util/PrometheusClient.py` | All Prometheus queries (latency p50/p90/p99, QPS, CPU, memory) |
| `util/KubernetesClient.py` | K8s API: list deployments, get/set replica counts |
| `util/GA.py` | Genetic algorithm using `geatpy` library |
| `monitor/MetricCollect.py` | Post-experiment metric export to CSV files |
| `simulation/RandomForestClassify.py` | Train the SLO violation predictor used by GA fitness |
| `config/Config.py` | Singleton config for all runtime parameters |

### Baseline Controllers (`others/`)

Selectable via `initController()` in `main.py`:
- `'PBScaler'` — main algorithm
- `'MicroScaler'` — Bayesian optimization per service
- `'SHOWAR'` — PID controller with topology awareness
- `'KHPA'` — wraps Kubernetes HPA
- `'random'` / `None` — ablation baselines

### Benchmarks

Two test applications in `benchmarks/`:
- **Online Boutique** (`microservices-demo/`) — 10 microservices (Go, Python, etc.)
- **Train-Ticket** (`train-ticket/`) — 43 microservices (Java Spring Boot)

Deploy with: `kubectl apply -f <manifest>.yaml`

### Data Flow

```
Prometheus → PrometheusClient → PBScaler
                                    ├── Anomaly detection (t-test)
                                    ├── Root cause analysis (PageRank)
                                    └── GA optimization (RandomForest fitness)
                                            ↓
                                    KubernetesClient → scale deployments
                                            ↓
                                    MetricCollect → CSV output
```

## Known Issues

- `monitor/MetricCollect.py` has a bug: `os._dir.exists()` should be `os.path.exists()`
- Training data paths in `simulation/RandomForestClassify.py` are hardcoded
- No connectivity checks for Prometheus or Kubernetes on startup
- `main.py` simulation model path is hardcoded and must be updated per environment
