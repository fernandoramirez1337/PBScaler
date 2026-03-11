#!/usr/bin/env python3
"""
Locust load test for Online Boutique with a staged ramp (10→200 users, 10 min).
Run headless:
  locust -f scripts/locustfile.py --headless \
    --host "http://${FRONTEND_IP}" \
    --run-time 10m \
    --csv "${OUT_DIR}/locust" --csv-full-history \
    --loglevel WARNING
"""

import random
from locust import HttpUser, task, between, LoadTestShape

PRODUCTS = [
    '0PUK6V6EV0',
    '1YMWWN1N4O',
    '2ZYFJ3GM2N',
    '66VCHSJNUP',
    '6E92ZMYYFZ',
    '9SIQT8TOJO',
    'L9ECAV7KIM',
    'LS4PSXUNUM',
    'OLJCESPC7Z',
]

CURRENCIES = ['EUR', 'USD', 'JPY', 'CAD']


class BoutiqueUser(HttpUser):
    wait_time = between(1, 5)

    @task(1)
    def browse_catalog(self):
        self.client.get("/")

    @task(10)
    def view_product(self):
        self.client.get("/product/" + random.choice(PRODUCTS))

    @task(2)
    def set_currency(self):
        self.client.post("/setCurrency", {"currency_code": random.choice(CURRENCIES)})

    @task(3)
    def view_cart(self):
        self.client.get("/cart")

    @task(2)
    def add_to_cart(self):
        product = random.choice(PRODUCTS)
        self.client.get("/product/" + product)
        self.client.post("/cart", {
            "product_id": product,
            "quantity": random.choice([1, 2, 3, 4, 5, 10]),
        })

    @task(1)
    def checkout(self):
        product = random.choice(PRODUCTS)
        self.client.get("/product/" + product)
        self.client.post("/cart", {
            "product_id": product,
            "quantity": random.choice([1, 2, 3, 4, 5, 10]),
        })
        self.client.post("/cart/checkout", {
            "email": "someone@example.com",
            "street_address": "1600 Amphitheatre Parkway",
            "zip_code": "94043",
            "city": "Mountain View",
            "state": "CA",
            "country": "United States",
            "credit_card_number": "4432-8015-6152-0454",
            "credit_card_expiration_month": "1",
            "credit_card_expiration_year": "2039",
            "credit_card_cvv": "672",
        })


class StagedRampShape(LoadTestShape):
    """
    Staged ramp: 10→200 users over 10 minutes.

    Elapsed (s)  | Users | Spawn rate
    -------------|-------|----------
    0   – 120    |  10   | 10
    120 – 240    |  50   |  5
    240 – 360    | 100   |  5
    360 – 480    | 150   |  5
    480 – 600    | 200   |  5
    > 600        | stop  | —
    """

    stages = [
        {"duration": 120, "users": 10,  "spawn_rate": 10},
        {"duration": 240, "users": 50,  "spawn_rate": 5},
        {"duration": 360, "users": 100, "spawn_rate": 5},
        {"duration": 480, "users": 150, "spawn_rate": 5},
        {"duration": 600, "users": 200, "spawn_rate": 5},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None
