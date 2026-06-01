"""Train SLO-violation Random Forest classifier for PBScaler.

Upstream WHU-AISE/PBScaler ships this with the OB svcs list active and
the TT svcs list commented. The fork keeps that as the default but adds
a `--benchmark {boutique,train_ticket}` flag so we can train both
without code changes.

Output paths (under this directory):
    boutique     -> boutique/RandomForestClassify.model
    train_ticket -> train_ticket/RandomForestClassify.model
                    + train_ticket/rf.pkl (ROC dump, upstream behaviour)
"""

import argparse
import os
import pandas as pd
import numpy as np
from matplotlib import pyplot as plt

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from sklearn.model_selection import GridSearchCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
from sklearn.metrics import roc_auc_score
from sklearn.metrics import recall_score
from sklearn.metrics import roc_curve
import joblib
import random
import pickle

# Service lists per benchmark — exactly as in upstream PBScaler RandomForestClassify.py
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

DEFAULT_TRAINING_DATA = {
    'boutique':     '../train_data/boutique/real_trace_5s_2.0.csv',
    'train_ticket': '../train_data/train_ticket/real_trace.csv',
}

DEFAULT_MODEL_OUT = {
    'boutique':     'boutique/RandomForestClassify.model',
    'train_ticket': 'train_ticket/RandomForestClassify.model',
}

DEFAULT_ROC_OUT = {
    'boutique':     'boutique/rf.pkl',
    'train_ticket': 'train_ticket/rf.pkl',
}


def data_loader(path, svcs):
    df = pd.read_csv(path)
    cols = [col for col in df.columns if col.endswith('&qps') or col.endswith('&count') or col == 'slo_reward']
    df = df[cols].fillna(0)
    # build dataset
    datas = []
    for _, row in df.iterrows():
        x = []
        for i in range(len(svcs)):
            svc = svcs[i]
            try:
                x.extend([i, row[svc + '&qps'], row[svc + '&count']])
            except KeyError:
                x.extend([i, 0, row[svc + '&count']]) if (svc + '&count') in row else x.extend([i, 0, 0])
        x.append(row['slo_reward'])
        datas.append(x)
    random.shuffle(datas)
    print(f'  rows: {len(datas)}')
    datas = np.array(datas)
    datas_x, datas_y = datas[:, 0:-1], datas[:, -1]
    return datas_x, datas_y


def setup_seed(seed: int):
    np.random.seed(seed)
    random.seed(seed)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--benchmark', choices=['boutique', 'train_ticket'], default='boutique')
    ap.add_argument('--training-data', default=None,
                    help='CSV path; defaults to DEFAULT_TRAINING_DATA[benchmark]')
    ap.add_argument('--model-out', default=None,
                    help='joblib output path; defaults to DEFAULT_MODEL_OUT[benchmark]')
    ap.add_argument('--seed', type=int, default=20)
    args = ap.parse_args()

    setup_seed(args.seed)
    svcs = SVCS_BY_BENCHMARK[args.benchmark]
    train_path = args.training_data or os.path.join(_SCRIPT_DIR, DEFAULT_TRAINING_DATA[args.benchmark])
    model_path = args.model_out or os.path.join(_SCRIPT_DIR, DEFAULT_MODEL_OUT[args.benchmark])
    roc_path = os.path.join(_SCRIPT_DIR, DEFAULT_ROC_OUT[args.benchmark])

    print(f'benchmark={args.benchmark}')
    print(f'  svcs: {len(svcs)}')
    print(f'  training_data: {train_path}')
    print(f'  model_out: {model_path}')

    model = RandomForestClassifier()
    datas_x, datas_y = data_loader(train_path, svcs)
    X_train, X_test, y_train, y_test = train_test_split(datas_x, datas_y)

    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    print(f"Test set score: {model.score(X_test, y_test):.4f}")
    print(f'  acc:    {accuracy_score(y_test, y_pred):.4f}')
    print(f'  recall: {recall_score(y_test, y_pred, pos_label=0):.4f}')
    print(f'  auc:    {roc_auc_score(y_test, y_pred):.4f}')

    fpr, tpr, thresholds = roc_curve(y_test, y_pred, pos_label=None, sample_weight=None, drop_intermediate=True)
    res = {'fpr': fpr, 'tpr': tpr, 'thresholds': thresholds}
    os.makedirs(os.path.dirname(roc_path), exist_ok=True)
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    pickle.dump(res, open(roc_path, 'wb'))
    joblib.dump(model, model_path)
    print(f'  ROC dump  -> {roc_path}')
    print(f'  Model     -> {model_path}')


if __name__ == '__main__':
    main()
