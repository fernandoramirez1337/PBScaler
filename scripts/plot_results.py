#!/usr/bin/env python3
"""
Plot KHPA baseline experiment results from CSVs.

Usage:
  python scripts/plot_results.py results/khpa_baseline [--slo 200]
"""

import argparse
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd


def _elapsed_minutes(df: pd.DataFrame) -> pd.Series:
    """Convert the 'timestamp' column to elapsed minutes from the first value."""
    ts = pd.to_datetime(df['timestamp'])
    return (ts - ts.iloc[0]).dt.total_seconds() / 60


def plot_latency(results_dir: str, slo: float):
    latency_path = os.path.join(results_dir, 'latency.csv')
    p95_path = os.path.join(results_dir, 'latency_p95.csv')

    if not os.path.exists(latency_path):
        print(f'  [skip] {latency_path} not found')
        return

    lat_df = pd.read_csv(latency_path)
    elapsed = _elapsed_minutes(lat_df)

    fig, ax = plt.subplots(figsize=(10, 4))

    if 'frontend&p50' in lat_df.columns:
        ax.plot(elapsed, lat_df['frontend&p50'], color='blue', label='frontend p50')
    if 'frontend&p90' in lat_df.columns:
        ax.plot(elapsed, lat_df['frontend&p90'], color='green', linestyle='--', label='frontend p90')

    if os.path.exists(p95_path):
        p95_df = pd.read_csv(p95_path)
        p95_elapsed = _elapsed_minutes(p95_df)
        if 'frontend&p95' in p95_df.columns:
            ax.plot(p95_elapsed, p95_df['frontend&p95'], color='orange', label='frontend p95')

    ax.axhline(y=slo, color='red', linestyle='--', label=f'SLO {slo} ms')
    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Latency (ms)')
    ax.set_title('Frontend Latency Over Time')
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(results_dir, 'latency_over_time.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_replicas(results_dir: str):
    path = os.path.join(results_dir, 'instances.csv')
    if not os.path.exists(path):
        print(f'  [skip] {path} not found')
        return

    df = pd.read_csv(path)
    elapsed = _elapsed_minutes(df)

    service_cols = [c for c in df.columns if c != 'timestamp']

    fig, ax = plt.subplots(figsize=(10, 4))
    for col in service_cols[:10]:
        label = col.replace('&count', '')
        ax.plot(elapsed, df[col], label=label)

    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Replica count')
    ax.set_title('Replica Count Over Time')
    ax.legend(bbox_to_anchor=(1.01, 1), loc='upper left', fontsize='small')
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(results_dir, 'replica_count_over_time.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_slo_violations(results_dir: str):
    path = os.path.join(results_dir, 'slo_violations.csv')
    if not os.path.exists(path):
        print(f'  [skip] {path} not found')
        return

    try:
        df = pd.read_csv(path)
    except Exception:
        print(f'  [skip] {path} is empty or unreadable')
        return
    if df.empty or len(df.columns) < 2:
        print(f'  [skip] {path} has no service data')
        return
    elapsed = _elapsed_minutes(df)

    service_cols = [c for c in df.columns if c != 'timestamp']
    data = df[service_cols].clip(lower=0).fillna(0)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.stackplot(elapsed, [data[c] for c in service_cols], labels=service_cols)
    ax.set_xlabel('Elapsed (minutes)')
    ax.set_ylabel('Requests > 500 ms / min')
    ax.set_title('SLO Violations Over Time')
    ax.legend(bbox_to_anchor=(1.01, 1), loc='upper left', fontsize='small')
    ax.grid(True)

    plt.tight_layout()
    out_path = os.path.join(results_dir, 'slo_violations_over_time.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def plot_resource(results_dir: str):
    path = os.path.join(results_dir, 'resource.csv')
    if not os.path.exists(path):
        print(f'  [skip] {path} not found')
        return

    df = pd.read_csv(path)
    elapsed = _elapsed_minutes(df)

    fig, ax1 = plt.subplots(figsize=(10, 4))
    ax2 = ax1.twinx()

    if 'vCPU' in df.columns:
        ax1.plot(elapsed, df['vCPU'], color='blue', label='vCPU')
    if 'memory' in df.columns:
        ax2.plot(elapsed, df['memory'], color='orange', label='Memory (MB)')

    ax1.set_xlabel('Elapsed (minutes)')
    ax1.set_ylabel('vCPU', color='blue')
    ax2.set_ylabel('Memory (MB)', color='orange')
    ax1.set_title('Resource Utilization Over Time')

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2)
    ax1.grid(True)

    plt.tight_layout()
    out_path = os.path.join(results_dir, 'resource_over_time.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  Wrote {out_path}')


def main():
    parser = argparse.ArgumentParser(description='Plot KHPA baseline experiment results')
    parser.add_argument('results_dir', nargs='?', default='results/khpa_baseline',
                        help='Directory containing result CSVs')
    parser.add_argument('--slo', type=float, default=200.0, help='SLO threshold in ms')
    args = parser.parse_args()

    results_dir = args.results_dir
    if not os.path.isabs(results_dir):
        results_dir = os.path.join(os.getcwd(), results_dir)

    if not os.path.isdir(results_dir):
        print(f'Error: {results_dir} is not a directory')
        raise SystemExit(1)

    print(f'Plotting results from {results_dir}  (SLO={args.slo} ms)')
    plot_latency(results_dir, args.slo)
    plot_replicas(results_dir)
    plot_slo_violations(results_dir)
    plot_resource(results_dir)
    print('Done.')


if __name__ == '__main__':
    main()
