"""Example storefront worker for Larkspur Supply Co. that uses the Flagship SDK.

It serves synthetic shoppers in a loop, asks the SDK which features each one gets, and reports
checkout conversions back for the experiment readout. Every evaluation is local; stop the
Flagship server and this keeps running on the last known good ruleset.

  FLAGSHIP_URL=http://127.0.0.1:8788 FLAGSHIP_SDK_KEY=sdk-prod-demo uv run python examples/shop/app.py
"""

from __future__ import annotations

import json
import os
import random
import time

from flagship.sdk import FlagshipClient

client = FlagshipClient(
    os.environ.get("FLAGSHIP_URL", "http://127.0.0.1:8788"),
    os.environ.get("FLAGSHIP_SDK_KEY", "sdk-prod-demo"),
    cache_path=os.environ.get("FLAGSHIP_CACHE", ".flagship-cache.json"),
)


def render_checkout(user: dict) -> str:
    if client.variation("new-checkout", user, default=False):
        return "one-page checkout"
    return "three-step checkout"


def render_home(user: dict) -> list[str]:
    parts = ["header"]
    if client.is_enabled("legacy-banner", user):
        parts.append("spring sale banner")
    if client.is_enabled("holiday-promo-2025", user):
        parts.append("holiday promo")
    if client.is_enabled("gift-wrap", user):  # not created on the server yet, always the default
        parts.append("gift wrap upsell")
    parts.append("pricing: " + client.variation("pricing-page-layout", user, default="classic"))
    return parts


def search(user: dict, query: str) -> str:
    ranking = "v2" if client.is_enabled("search-v2", user) else "v1"
    return f"results for {query!r} ranked by {ranking}"


def cart_limit(user: dict) -> int:
    return client.variation("max-cart-items", user, default=50)


def main() -> None:
    rng = random.Random(int(os.environ.get("SHOP_SEED", "3")))
    interval = float(os.environ.get("SHOP_INTERVAL", "0.5"))
    i = 0
    while True:
        user = {"key": f"shopper-{rng.randint(1, 5000):05d}", "country": rng.choice(["US", "CA", "GB", "DE"])}
        checkout = render_checkout(user)
        converted = rng.random() < (0.11 if checkout == "one-page checkout" else 0.10)
        if converted:
            client.track("checkout_completed", user)
        if i % 10 == 0:
            line = {
                "user": user["key"],
                "home": render_home(user),
                "search": search(user, "garden hose"),
                "cart_limit": cart_limit(user),
                "checkout": checkout,
                "sdk": client.status(),
            }
            print(json.dumps(line), flush=True)
        i += 1
        time.sleep(interval)


if __name__ == "__main__":
    main()
