#!/usr/bin/env python3
"""
Generate training data for the RandomForest SLO-violation predictor
from existing experiment results (e.g. KHPA baseline).

Usage:
  python scripts/generate_training_data.py \
    --results-dir results/khpa_baseline \
    --slo 200 \
    --out train_data/boutique/real_trace_5s_2.0.csv

Then train the model:
  cd simulation && python RandomForestClassify.py
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SVCS = [
    'adservice', 'cartservice', 'checkoutservice', 'currencyservice',
    'emailservice', 'frontend', 'paymentservice', 'productcatalogservice',
    'recommendationservice', 'shippingservice',
]


def main():
    parser = argparse.ArgumentParser(description='Generate RandomForest training data from experiment results')
    parser.add_argument('--results-dir', default='results/khpa_baseline',
                        help='Directory with svc_qps.csv, instances.csv, latency.csv')
    parser.add_argument('--slo', type=float, default=200.0, help='SLO threshold in ms')
    parser.add_argument('--out', default='train_data/boutique/real_trace_5s_2.0.csv',
                        help='Output CSV path')
    parser.add_argument('--n-synthetic', type=int, default=10,
                        help='Number of synthetic variations per real row')
    parser.add_argument('--max-replicas', type=int, default=5,
                        help='Max replica count for synthetic variations')
    args = parser.parse_args()

    results_dir = args.results_dir
    if not os.path.isabs(results_dir):
        results_dir = os.path.join(os.getcwd(), results_dir)

    # Read source CSVs
    qps_df = pd.read_csv(os.path.join(results_dir, 'svc_qps.csv'))
    inst_df = pd.read_csv(os.path.join(results_dir, 'instances.csv'))
    lat_df = pd.read_csv(os.path.join(results_dir, 'latency.csv'))

    # Rename QPS columns to {svc}&qps format
    qps_renamed = {'timestamp': 'timestamp'}
    for svc in SVCS:
        if svc in qps_df.columns:
            qps_renamed[svc] = svc + '&qps'
    qps_df = qps_df.rename(columns=qps_renamed)

    # Rename instance columns — already in {svc}&count format
    # Keep only the services we care about
    inst_cols = ['timestamp'] + [svc + '&count' for svc in SVCS if svc + '&count' in inst_df.columns]
    inst_df = inst_df[inst_cols]

    # Get frontend p90 for SLO label
    lat_cols = ['timestamp', 'frontend&p90']
    lat_df = lat_df[[c for c in lat_cols if c in lat_df.columns]]

    # Merge on timestamp
    merged = qps_df.merge(inst_df, on='timestamp', how='inner')
    merged = merged.merge(lat_df, on='timestamp', how='inner')

    # Compute slo_reward: 1 if frontend p90 < SLO, else 0
    merged['slo_reward'] = (merged['frontend&p90'] < args.slo).astype(int)

    # Build the real rows with only the columns the model expects
    keep_cols = []
    for svc in SVCS:
        qps_col = svc + '&qps'
        count_col = svc + '&count'
        if qps_col in merged.columns:
            keep_cols.append(qps_col)
        if count_col in merged.columns:
            keep_cols.append(count_col)
    keep_cols.append('slo_reward')

    real_data = merged[keep_cols].fillna(0)

    # Generate synthetic variations
    rng = np.random.default_rng(42)
    synthetic_rows = []

    count_cols = [svc + '&count' for svc in SVCS if svc + '&count' in real_data.columns]

    for _, row in real_data.iterrows():
        # Keep original row
        synthetic_rows.append(row.values.copy())

        # Generate N synthetic variations with random replica counts
        for _ in range(args.n_synthetic):
            new_row = row.values.copy()
            col_indices = [real_data.columns.get_loc(c) for c in count_cols]
            random_counts = rng.integers(1, args.max_replicas + 1, size=len(col_indices))
            for idx, count in zip(col_indices, random_counts):
                new_row[idx] = float(count)

            # Heuristic for slo_reward: higher total replicas -> more likely to meet SLO
            total_replicas = sum(new_row[idx] for idx in col_indices)
            orig_total = sum(row.values[idx] for idx in col_indices)
            orig_reward = row['slo_reward']

            if orig_reward == 1:
                # Was meeting SLO — fewer replicas may cause violation
                if total_replicas >= orig_total * 0.7:
                    new_row[-1] = 1.0
                else:
                    new_row[-1] = float(rng.random() < 0.4)
            else:
                # Was violating SLO — more replicas may help
                if total_replicas >= orig_total * 1.5:
                    new_row[-1] = float(rng.random() < 0.7)
                else:
                    new_row[-1] = float(rng.random() < 0.2)

            synthetic_rows.append(new_row)

    result_df = pd.DataFrame(synthetic_rows, columns=real_data.columns)

    # Write output
    out_path = args.out
    if not os.path.isabs(out_path):
        out_path = os.path.join(os.getcwd(), out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    result_df.to_csv(out_path, index=False)

    print(f'Generated {len(result_df)} rows ({len(real_data)} real + {len(result_df) - len(real_data)} synthetic)')
    print(f'  SLO reward distribution: {result_df["slo_reward"].value_counts().to_dict()}')
    print(f'  Written to {out_path}')


if __name__ == '__main__':
    main()
