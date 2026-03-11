#!/usr/bin/env python3
"""
Post-experiment Prometheus metric collector for the KHPA baseline.

Usage:
  python scripts/collect_metrics.py \
    --start <unix_ts> --end <unix_ts> \
    --namespace online-boutique \
    --out results/khpa_baseline
"""

import argparse
import os
import sys

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from config.Config import Config
import monitor.MetricCollect as MetricCollect
from util.PrometheusClient import PrometheusClient


def collect_p95_latency(config: Config, out_dir: str):
    prom_util = PrometheusClient(config)
    sql = (
        'histogram_quantile(0.95, sum(irate('
        'istio_request_duration_milliseconds_bucket{'
        'reporter="destination", '
        'destination_workload_namespace="%s"'
        '}[1m])) by (destination_workload, destination_workload_namespace, le))'
        % config.namespace
    )
    results = prom_util.execute_prom(config.prom_range_url, sql)

    df = pd.DataFrame()
    for result in results:
        name = result['metric']['destination_workload']
        values = result['values']
        values = list(zip(*values))
        if 'timestamp' not in df:
            df['timestamp'] = values[0]
            df['timestamp'] = df['timestamp'].astype('datetime64[s]')
        key = name + '&p95'
        df[key] = pd.Series(values[1])
        df[key] = df[key].astype('float64')

    df.to_csv(os.path.join(out_dir, 'latency_p95.csv'), index=False)


def collect_slo_violations(config: Config, out_dir: str):
    """Count requests > 500 ms per minute per service."""
    prom_util = PrometheusClient(config)
    ns = config.namespace
    sql = (
        'sum(increase(istio_request_duration_milliseconds_bucket{'
        'reporter="destination", namespace="%s", le="+Inf"}[1m])) by (destination_workload)'
        ' - '
        'sum(increase(istio_request_duration_milliseconds_bucket{'
        'reporter="destination", namespace="%s", le="500"}[1m])) by (destination_workload)'
        % (ns, ns)
    )
    results = prom_util.execute_prom(config.prom_range_url, sql)

    df = pd.DataFrame()
    for result in results:
        name = result['metric']['destination_workload']
        values = result['values']
        values = list(zip(*values))
        if 'timestamp' not in df:
            df['timestamp'] = values[0]
            df['timestamp'] = df['timestamp'].astype('datetime64[s]')
        df[name] = pd.Series(values[1])
        df[name] = df[name].astype('float64')

    df.to_csv(os.path.join(out_dir, 'slo_violations.csv'), index=False)


def main():
    parser = argparse.ArgumentParser(description='Collect post-experiment metrics from Prometheus')
    parser.add_argument('--start', type=int, required=True, help='Start Unix timestamp')
    parser.add_argument('--end', type=int, required=True, help='End Unix timestamp')
    parser.add_argument('--namespace', default='online-boutique', help='Kubernetes namespace')
    parser.add_argument('--out', default='results/khpa_baseline', help='Output directory')
    parser.add_argument(
        '--prom-range-url',
        default=os.environ.get('PROM_RANGE_URL', 'http://localhost:9090/api/v1/query_range'),
        help='Prometheus range query URL',
    )
    parser.add_argument(
        '--prom-query-url',
        default=os.environ.get('PROM_QUERY_URL', 'http://localhost:9090/api/v1/query'),
        help='Prometheus instant query URL',
    )
    args = parser.parse_args()

    out_dir = args.out
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(os.getcwd(), out_dir)
    os.makedirs(out_dir, exist_ok=True)

    config = Config()
    config.namespace = args.namespace
    config.prom_range_url = args.prom_range_url
    config.prom_no_range_url = args.prom_query_url
    config.start = args.start
    config.end = args.end

    print(f'Collecting metrics: {args.start} → {args.end}  ns={args.namespace}  out={out_dir}')

    # Standard CSVs via MetricCollect
    MetricCollect.collect(config, out_dir)

    # Additional metrics
    print('Collecting p95 latency...')
    collect_p95_latency(config, out_dir)

    print('Collecting SLO violations...')
    collect_slo_violations(config, out_dir)

    print(f'Done. Results written to {out_dir}')


if __name__ == '__main__':
    main()
