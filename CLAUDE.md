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

# Run the autoscaler
python main.py

# Run the test suite (no live cluster needed — uses mock Prometheus + K8s)
pytest tests/test_pipeline.py -v

# Collect post-experiment metrics to CSV
# (called programmatically from MetricCollect.collect(config, config.data_dir))
```

## Configuration

All runtime parameters live in **`config.yaml`** at the project root. `config/Config.py` reads this file on startup; never edit Config.py directly.

```yaml
kubernetes:
  namespace: default
  kubeconfig: ~/.kube/config   # override with K8S_CONFIG env var

prometheus:
  range_url: http://localhost:9090/api/v1/query_range   # override with PROM_RANGE_URL
  query_url:  http://localhost:9090/api/v1/query        # override with PROM_QUERY_URL
  step: 5

autoscaler:
  slo: 200          # target p90 latency in ms
  max_pod: 8
  min_pod: 1
  duration: 1200    # seconds
  simulation_model: simulation/boutique/RandomForestClassify.model  # relative to project root

output:
  data_dir: output
```

Environment variables (`K8S_NAMESPACE`, `K8S_CONFIG`, `PROM_RANGE_URL`, `PROM_QUERY_URL`) take precedence over `config.yaml` values.

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
| `util/PCAUtil.py` | PCA dimensionality reduction helpers |
| `util/Spectrum.py` | Spectral analysis utilities |
| `monitor/MetricCollect.py` | Post-experiment metric export to CSV files |
| `simulation/RandomForestClassify.py` | Train the SLO violation predictor used by GA fitness |
| `config/Config.py` | Loads `config.yaml`; exposes runtime parameters |
| `evaluation/Evaluation.py` | Metric evaluation and comparison across experiments |
| `evaluation/Draw.py` | Plotting helpers for evaluation results |

### Baseline Controllers (`others/`)

Selectable via `initController()` in `main.py`:
- `'PBScaler'` — main algorithm
- `'MicroScaler'` — Bayesian optimization per service
- `'SHOWAR'` — PID controller with topology awareness
- `'KHPA'` — wraps Kubernetes HPA
- `'random'` — random scaling baseline

### RL Module (`RL/`)

An experimental reinforcement learning branch for autoscaling (not used by `main.py`):

- `RL/Environment.py` — Gym-style environment wrapping Prometheus + K8s
- `RL/Simulation.py` — Simulation harness for RL training
- `RL/common/` — Shared GNN components: `GAT.py`, `MPNN.py`, `StateModel.py`
- `RL/film/` — Standard RL agents: `D3QN`, `DDPG`, `TD3`, actor-critic
- `RL/grScaler/` — Graph-aware RL agents (`GrScaler_D3QN`, `GrScaler_TD3`, `GraScaler_DDPG`)

### Benchmarks

Two test applications in `benchmarks/`:
- **Online Boutique** (`microservices-demo/`) — 10 microservices (Go, Python, etc.)
- **Train-Ticket** (`train-ticket/`) — 43 microservices (Java Spring Boot)

Deploy with: `kubectl apply -f <manifest>.yaml`

### Data Flow

```
config.yaml → Config → PBScaler
Prometheus  → PrometheusClient → PBScaler
                                    ├── Anomaly detection (t-test)
                                    ├── Root cause analysis (PageRank)
                                    └── GA optimization (RandomForest fitness)
                                            ↓
                                    KubernetesClient → scale deployments
                                            ↓
                                    MetricCollect → CSV output
```

## Tests

`tests/test_pipeline.py` covers the three pipeline phases without a live cluster:

- **Phase 1** — `TestAnomalyDetection`: verifies `get_abnormal_calls()` for normal/single/cascading scenarios
- **Phase 2** — `TestRootCauseAnalysis`: verifies PageRank surfaces the correct root-cause service
- **Phase 3** — `TestGAOptimisation`: verifies `choose_action('add')` calls `patch_scale()` within bounds
- **TestScenarioSwitching**: verifies the mock server switches scenarios at runtime

Mocks live in `tests/mocks/`: `MockPrometheusServer` (real HTTP, no live Prometheus), `MockKubernetesClient`, and `SCENARIOS` (normal_load, single_bottleneck, cascading_bottleneck). `GA` is replaced with a lightweight `MockGA` to avoid the `geatpy` / model-file dependency.

## Known Issues

- Training data paths in `simulation/RandomForestClassify.py` are hardcoded
- No connectivity checks for Prometheus or Kubernetes on startup
- `RL/Environment.py` hardcodes `redis-cart` node removal and `SLO=200`, bypassing `config.yaml`
