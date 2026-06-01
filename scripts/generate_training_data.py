#!/usr/bin/env python3
"""
Generate training data for the RandomForest SLO-violation predictor
from existing experiment results (e.g. KHPA baseline, or even prior PBScaler runs).

Output is a CSV in the format `simulation/RandomForestClassify.py` expects:
columns `<svc>&qps`, `<svc>&count`, `slo_reward`.

Usage:
  # OB (default)
  python scripts/generate_training_data.py \
    --benchmark boutique \
    --results-dir results/khpa_baseline \
    --slo 200 \
    --out train_data/boutique/real_trace_5s_2.0.csv

  # TT (concat multiple runs)
  python scripts/generate_training_data.py \
    --benchmark train_ticket \
    --results-dir results/sprint-1/train-ticket/step/run1 \
                  results/sprint-1/train-ticket/step/run2 \
                  results/sprint-1/train-ticket/step/run3 \
                  results/sprint-1/train-ticket/bursty/run1 \
                  results/sprint-1/train-ticket/bursty/run2 \
                  results/sprint-1/train-ticket/bursty/run3 \
    --slo 500 \
    --slo-reference-svc ts-travel-service \
    --out train_data/train_ticket/real_trace.csv

Then train the model:
  cd simulation && python RandomForestClassify.py --benchmark train_ticket
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Service lists kept in sync with simulation/RandomForestClassify.py
SVCS_BY_BENCHMARK = {
    'boutique': [
        'adservice', 'cartservice', 'checkoutservice', 'currencyservice',
        'emailservice', 'frontend', 'paymentservice', 'productcatalogservice',
        'recommendationservice', 'shippingservice',
    ],
    'train_ticket': [
        'ts-admin-basic-info-service', 'ts-admin-order-service', 'ts-admin-route-service',
        'ts-admin-travel-service', 'ts-admin-user-service', 'ts-assurance-service',
        'ts-auth-service', 'ts-avatar-service', 'ts-basic-service', 'ts-cancel-service',
        'ts-config-service', 'ts-consign-price-service', 'ts-consign-service',
        'ts-contacts-service', 'ts-delivery-service', 'ts-execute-service',
        'ts-food-map-service', 'ts-food-service', 'ts-inside-payment-service',
        'ts-news-service', 'ts-notification-service', 'ts-order-other-service',
        'ts-order-service', 'ts-payment-service', 'ts-preserve-other-service',
        'ts-preserve-service', 'ts-price-service', 'ts-rebook-service',
        'ts-route-plan-service', 'ts-route-service', 'ts-seat-service',
        'ts-security-service', 'ts-station-service', 'ts-ticket-office-service',
        'ts-ticketinfo-service', 'ts-train-service', 'ts-travel-plan-service',
        'ts-travel-service', 'ts-travel2-service', 'ts-ui-dashboard',
        'ts-user-service', 'ts-verification-code-service', 'ts-voucher-service',
    ],
}


def process_one_run(results_dir: str, svcs: list, slo_threshold_ms: float,
                    slo_reference_svc: str) -> pd.DataFrame:
    """Read one run's CSVs and return real_data DataFrame with slo_reward."""
    qps_df = pd.read_csv(os.path.join(results_dir, 'svc_qps.csv'))
    inst_df = pd.read_csv(os.path.join(results_dir, 'instances.csv'))
    lat_df = pd.read_csv(os.path.join(results_dir, 'latency.csv'))

    # Rename QPS columns to {svc}&qps format
    qps_renamed = {'timestamp': 'timestamp'}
    for svc in svcs:
        if svc in qps_df.columns:
            qps_renamed[svc] = svc + '&qps'
    qps_df = qps_df.rename(columns=qps_renamed)

    # Instance columns are already {svc}&count
    inst_cols = ['timestamp'] + [svc + '&count' for svc in svcs if svc + '&count' in inst_df.columns]
    inst_df = inst_df[inst_cols]

    # SLO reference column
    ref_col = f'{slo_reference_svc}&p90'
    if ref_col not in lat_df.columns:
        raise ValueError(
            f'{results_dir}: latency.csv missing reference column {ref_col!r}. '
            f'Available: {[c for c in lat_df.columns if c.endswith("&p90")][:5]}...'
        )
    lat_df = lat_df[['timestamp', ref_col]]

    merged = qps_df.merge(inst_df, on='timestamp', how='inner')
    merged = merged.merge(lat_df, on='timestamp', how='inner')

    # Compute slo_reward: 1 if reference svc p90 < SLO, else 0
    merged['slo_reward'] = (merged[ref_col] < slo_threshold_ms).astype(int)

    # Build the real rows in the columns the model expects
    keep_cols = []
    for svc in svcs:
        qps_col = svc + '&qps'
        count_col = svc + '&count'
        if qps_col in merged.columns:
            keep_cols.append(qps_col)
        if count_col in merged.columns:
            keep_cols.append(count_col)
    keep_cols.append('slo_reward')

    return merged[keep_cols].fillna(0)


def main():
    parser = argparse.ArgumentParser(description='Generate RandomForest training data',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--benchmark', choices=['boutique', 'train_ticket'], default='boutique')
    parser.add_argument('--results-dir', nargs='+', required=True,
                        help='One or more dirs with svc_qps.csv, instances.csv, latency.csv')
    parser.add_argument('--slo', type=float, default=200.0, help='SLO threshold in ms')
    parser.add_argument('--slo-reference-svc', default=None,
                        help='Service whose p90 is the SLO indicator. '
                             'Defaults: boutique -> frontend, train_ticket -> ts-travel-service')
    parser.add_argument('--out', required=True, help='Output CSV path')
    parser.add_argument('--n-synthetic', type=int, default=10,
                        help='Number of synthetic variations per real row')
    parser.add_argument('--max-replicas', type=int, default=5,
                        help='Max replica count for synthetic variations')
    args = parser.parse_args()

    svcs = SVCS_BY_BENCHMARK[args.benchmark]
    if args.slo_reference_svc is None:
        args.slo_reference_svc = 'frontend' if args.benchmark == 'boutique' else 'ts-travel-service'

    print(f'benchmark: {args.benchmark}')
    print(f'  svcs: {len(svcs)}')
    print(f'  slo: {args.slo}ms (reference: {args.slo_reference_svc}&p90)')
    print(f'  results-dirs: {len(args.results_dir)}')

    # Load and concatenate all runs
    real_frames = []
    for d in args.results_dir:
        if not os.path.isabs(d):
            d = os.path.join(os.getcwd(), d)
        try:
            df = process_one_run(d, svcs, args.slo, args.slo_reference_svc)
            print(f'  + {d}: {len(df)} rows, slo_reward dist {df.slo_reward.value_counts().to_dict()}')
            real_frames.append(df)
        except (FileNotFoundError, ValueError) as exc:
            print(f'  - SKIP {d}: {exc}')

    if not real_frames:
        print('ERROR: no usable results dirs')
        sys.exit(1)

    real_data = pd.concat(real_frames, ignore_index=True).fillna(0)
    print(f'  combined real rows: {len(real_data)}')

    # Generate synthetic variations
    rng = np.random.default_rng(42)
    synthetic_rows = []
    count_cols = [svc + '&count' for svc in svcs if svc + '&count' in real_data.columns]
    col_indices = [real_data.columns.get_loc(c) for c in count_cols]

    for _, row in real_data.iterrows():
        synthetic_rows.append(row.values.copy())
        for _ in range(args.n_synthetic):
            new_row = row.values.copy()
            random_counts = rng.integers(1, args.max_replicas + 1, size=len(col_indices))
            for idx, count in zip(col_indices, random_counts):
                new_row[idx] = float(count)
            total_replicas = sum(new_row[idx] for idx in col_indices)
            orig_total = sum(row.values[idx] for idx in col_indices)
            orig_reward = row['slo_reward']
            if orig_reward == 1:
                if total_replicas >= orig_total * 0.7:
                    new_row[-1] = 1.0
                else:
                    new_row[-1] = float(rng.random() < 0.4)
            else:
                if total_replicas >= orig_total * 1.5:
                    new_row[-1] = float(rng.random() < 0.7)
                else:
                    new_row[-1] = float(rng.random() < 0.2)
            synthetic_rows.append(new_row)

    result_df = pd.DataFrame(synthetic_rows, columns=real_data.columns)

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
