#!/usr/bin/env python3
"""
Generate comparison charts between KHPA and PBScaler experiment results.

Usage:
  python scripts/plot_comparison.py \
    --khpa-dir results/khpa_baseline \
    --pbscaler-dir results/pbscaler_baseline \
    --out results/comparison \
    --slo 200
"""

import argparse
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.Evaluation import SLA_conflict, resource_cost, pod_cost


def _elapsed_minutes(df: pd.DataFrame) -> pd.Series:
    ts = pd.to_datetime(df['timestamp'])
    return (ts - ts.iloc[0]).dt.total_seconds() / 60


def _load_csv(directory: str, filename: str) -> pd.DataFrame:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f'{path} not found')
    return pd.read_csv(path)


def plot_p99_latency(khpa_dir: str, pb_dir: str, out_dir: str, slo: float):
    khpa_lat = _load_csv(khpa_dir, 'latency.csv')
    pb_lat = _load_csv(pb_dir, 'latency.csv')

    fig, ax = plt.subplots(figsize=(10, 4))

    if 'frontend&p99' in khpa_lat.columns:
        ax.plot(_elapsed_minutes(khpa_lat), khpa_lat['frontend&p99'],
                color='blue', label='KHPA')
    if 'frontend&p99' in pb_lat.columns:
        ax.plot(_elapsed_minutes(pb_lat), pb_lat['frontend&p99'],
                color='orange', label='PBScaler')

    ax.axhline(y=slo, color='red', linestyle='--', label=f'SLO {slo} ms')
    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Latency (ms)')
    ax.set_title('P99 Latency Comparison: KHPA vs PBScaler')
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'p99_latency_comparison.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_slo_violations(khpa_dir: str, pb_dir: str, out_dir: str, slo: float):
    khpa_lat = _load_csv(khpa_dir, 'latency.csv')
    pb_lat = _load_csv(pb_dir, 'latency.csv')

    fig, ax = plt.subplots(figsize=(10, 4))

    for df, color, label in [(khpa_lat, 'blue', 'KHPA'), (pb_lat, 'orange', 'PBScaler')]:
        if 'frontend&p90' in df.columns:
            violations = (df['frontend&p90'] > slo).cumsum()
            ax.plot(_elapsed_minutes(df), violations, color=color, label=label)

    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Cumulative SLO Violations')
    ax.set_title('Cumulative SLO Violations (frontend p90 > SLO)')
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'slo_violations_comparison.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_replica_count(khpa_dir: str, pb_dir: str, out_dir: str):
    khpa_inst = _load_csv(khpa_dir, 'instances.csv')
    pb_inst = _load_csv(pb_dir, 'instances.csv')

    fig, ax = plt.subplots(figsize=(10, 4))

    for df, color, label in [(khpa_inst, 'blue', 'KHPA'), (pb_inst, 'orange', 'PBScaler')]:
        count_cols = [c for c in df.columns if c.endswith('&count')]
        total = df[count_cols].sum(axis=1)
        ax.plot(_elapsed_minutes(df), total, color=color, label=label)

    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Total Replica Count')
    ax.set_title('Total Replica Count Comparison: KHPA vs PBScaler')
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'avg_replica_count_comparison.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_cpu_utilization(khpa_dir: str, pb_dir: str, out_dir: str):
    khpa_res = _load_csv(khpa_dir, 'resource.csv')
    pb_res = _load_csv(pb_dir, 'resource.csv')

    fig, ax = plt.subplots(figsize=(10, 4))

    for df, color, label in [(khpa_res, 'blue', 'KHPA'), (pb_res, 'orange', 'PBScaler')]:
        if 'vCPU' in df.columns:
            ax.plot(_elapsed_minutes(df), df['vCPU'], color=color, label=label)

    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('vCPU')
    ax.set_title('CPU Utilization Comparison: KHPA vs PBScaler')
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'cpu_utilization_comparison.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_summary_bar(khpa_dir: str, pb_dir: str, out_dir: str, slo: float):
    khpa_latency_path = os.path.join(khpa_dir, 'latency.csv')
    pb_latency_path = os.path.join(pb_dir, 'latency.csv')
    khpa_resource_path = os.path.join(khpa_dir, 'resource.csv')
    pb_resource_path = os.path.join(pb_dir, 'resource.csv')
    khpa_instances_path = os.path.join(khpa_dir, 'instances.csv')
    pb_instances_path = os.path.join(pb_dir, 'instances.csv')

    # Avg P99 latency
    khpa_lat = pd.read_csv(khpa_latency_path)
    pb_lat = pd.read_csv(pb_latency_path)
    khpa_avg_p99 = khpa_lat['frontend&p99'].mean() if 'frontend&p99' in khpa_lat.columns else 0
    pb_avg_p99 = pb_lat['frontend&p99'].mean() if 'frontend&p99' in pb_lat.columns else 0

    # SLO violation rate
    khpa_viol = SLA_conflict(slo, khpa_latency_path) * 100
    pb_viol = SLA_conflict(slo, pb_latency_path) * 100

    # Avg total replicas
    khpa_pods = pod_cost(khpa_instances_path)
    pb_pods = pod_cost(pb_instances_path)

    # Resource cost
    khpa_cost = resource_cost(khpa_resource_path)
    pb_cost = resource_cost(pb_resource_path)

    metrics = ['Avg P99\nLatency (ms)', 'SLO Violation\nRate (%)', 'Avg Total\nReplicas', 'Resource\nCost ($)']
    khpa_vals = [khpa_avg_p99, khpa_viol, khpa_pods, khpa_cost]
    pb_vals = [pb_avg_p99, pb_viol, pb_pods, pb_cost]

    x = np.arange(len(metrics))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 4))
    bars1 = ax.bar(x - width / 2, khpa_vals, width, label='KHPA', color='blue', alpha=0.7)
    bars2 = ax.bar(x + width / 2, pb_vals, width, label='PBScaler', color='orange', alpha=0.7)

    ax.set_xticks(x)
    ax.set_xticklabels(metrics)
    ax.set_title('Summary Comparison: KHPA vs PBScaler')
    ax.legend()
    ax.grid(True, axis='y')

    # Add value labels on bars
    for bar in bars1:
        height = bar.get_height()
        ax.annotate(f'{height:.1f}', xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3), textcoords='offset points', ha='center', va='bottom', fontsize=8)
    for bar in bars2:
        height = bar.get_height()
        ax.annotate(f'{height:.1f}', xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3), textcoords='offset points', ha='center', va='bottom', fontsize=8)

    plt.tight_layout()
    out_path = os.path.join(out_dir, 'summary_bar_chart.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')

    # Print summary table
    print('\n  Summary:')
    print(f'  {"Metric":<25} {"KHPA":>10} {"PBScaler":>10}')
    print(f'  {"-"*45}')
    for m, k, p in zip(metrics, khpa_vals, pb_vals):
        m_clean = m.replace('\n', ' ')
        print(f'  {m_clean:<25} {k:>10.2f} {p:>10.2f}')


def main():
    parser = argparse.ArgumentParser(description='Generate KHPA vs PBScaler comparison charts')
    parser.add_argument('--khpa-dir', default='results/khpa_baseline',
                        help='Directory with KHPA baseline CSVs')
    parser.add_argument('--pbscaler-dir', default='results/pbscaler_baseline',
                        help='Directory with PBScaler baseline CSVs')
    parser.add_argument('--out', default='results/comparison',
                        help='Output directory for comparison PNGs')
    parser.add_argument('--slo', type=float, default=200.0, help='SLO threshold in ms')
    args = parser.parse_args()

    # Resolve relative paths
    for attr in ('khpa_dir', 'pbscaler_dir', 'out'):
        val = getattr(args, attr)
        if not os.path.isabs(val):
            setattr(args, attr, os.path.join(os.getcwd(), val))

    os.makedirs(args.out, exist_ok=True)

    print(f'Comparing KHPA ({args.khpa_dir}) vs PBScaler ({args.pbscaler_dir})')
    print(f'  SLO={args.slo} ms, output={args.out}\n')

    try:
        plot_p99_latency(args.khpa_dir, args.pbscaler_dir, args.out, args.slo)
    except FileNotFoundError as e:
        print(f'  [skip] P99 latency: {e}')

    try:
        plot_slo_violations(args.khpa_dir, args.pbscaler_dir, args.out, args.slo)
    except FileNotFoundError as e:
        print(f'  [skip] SLO violations: {e}')

    try:
        plot_replica_count(args.khpa_dir, args.pbscaler_dir, args.out)
    except FileNotFoundError as e:
        print(f'  [skip] Replica count: {e}')

    try:
        plot_cpu_utilization(args.khpa_dir, args.pbscaler_dir, args.out)
    except FileNotFoundError as e:
        print(f'  [skip] CPU utilization: {e}')

    try:
        plot_summary_bar(args.khpa_dir, args.pbscaler_dir, args.out, args.slo)
    except FileNotFoundError as e:
        print(f'  [skip] Summary bar: {e}')

    print('\nDone.')


if __name__ == '__main__':
    main()
