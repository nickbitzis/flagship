import math
import random

import pytest
from conftest import bool_flag

from flagship import stats
from flagship.seed import seed, simulate_experiment
from flagship.store import Store


def test_chi2_survival_matches_known_values():
    # Reference values from standard chi-square tables.
    assert stats.chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert stats.chi2_sf(10.828, 1) == pytest.approx(0.001, abs=1e-4)
    assert stats.chi2_sf(5.991, 2) == pytest.approx(0.05, abs=1e-3)
    assert stats.chi2_sf(27.877, 9) == pytest.approx(0.001, abs=1e-4)
    assert stats.chi2_sf(0, 3) == 1.0


def test_compare_lift_ci_and_p_value():
    r = stats.compare((1000, 10_000), (1100, 10_000))
    assert r["diff"] == pytest.approx(0.01)
    assert r["lift"] == pytest.approx(0.10)
    se = math.sqrt(0.1 * 0.9 / 10_000 + 0.11 * 0.89 / 10_000)
    assert r["ci_low"] == pytest.approx(0.01 - 1.959964 * se)
    assert r["ci_high"] == pytest.approx(0.01 + 1.959964 * se)
    assert r["p_value"] == pytest.approx(0.0217, abs=0.001)  # pooled two-proportion z test, z = 2.30
    assert r["significant"] is True
    assert stats.compare((100, 1000), (101, 1000))["significant"] is False


def test_srm_detects_a_broken_split():
    ok = stats.srm_check([5040, 4960], [50, 50])
    assert ok["mismatch"] is False and ok["p_value"] > 0.4
    bad = stats.srm_check([5300, 4700], [50, 50])  # chi2 = 36
    assert bad["mismatch"] is True and bad["p_value"] < 1e-6
    weighted = stats.srm_check([2000, 8000], [20_000, 80_000])  # 20/80 rollout is fine
    assert weighted["mismatch"] is False


def test_readout_shape():
    r = stats.readout(["a", "b"], [100, 100], [10, 20], [1, 1], control=0)
    assert r["variations"][0]["rate"] == 0.1 and "lift" not in r["variations"][0]
    assert r["variations"][1]["lift"] == pytest.approx(1.0)


def test_simulated_experiment_recovers_the_true_lift():
    s = Store(":memory:")
    seed(s, simulate=False)
    simulate_experiment(s, users=20_000, base_rate=0.10, relative_lift=0.12, seed=7)
    data = s.experiment_data("new-checkout", "prod")
    r = stats.readout(["one-page", "three-step"], data["exposures"], data["conversions"], data["weights"], control=1)
    treat = r["variations"][0]
    assert sum(data["exposures"]) == 20_000
    assert r["srm"]["mismatch"] is False
    assert treat["ci_low"] < 0.012 < treat["ci_high"]  # the true difference is inside the 95% CI
    assert treat["significant"] is True


def test_readout_flags_srm_and_counts_conversions_after_exposure_only(store):
    f = bool_flag("exp-a", experiment={"primary_metric": "buy", "control": 1})
    f["environments"]["prod"]["fallthrough"] = {"rollout": {"weights": [50_000, 50_000], "bucket_by": "key"}}
    store.create_flag(f, "alice")
    rng = random.Random(3)
    events = []
    for i in range(3000):
        var = 0 if rng.random() < 0.58 else 1  # a buggy client logs variation 0 far too often
        events.append({"type": "impression", "flag": "exp-a", "variation": var, "user_key": f"u{i}", "ts": 1000 + i})
    events.append({"type": "conversion", "metric": "buy", "user_key": "u5", "ts": 10})  # before exposure, ignored
    events.append({"type": "conversion", "metric": "buy", "user_key": "u6", "ts": 99_999})
    store.record_events("prod", events)
    data = store.experiment_data("exp-a", "prod")
    assert sum(data["conversions"]) == 1
    assert stats.srm_check(data["exposures"], data["weights"])["mismatch"] is True


def test_readout_endpoint(client):
    f = bool_flag("exp-b", experiment={"primary_metric": "buy", "control": 1})
    client.post("/api/flags", json=f, headers={"x-actor": "alice"})
    ev = [{"type": "impression", "flag": "exp-b", "variation": i % 2, "user_key": f"u{i}", "ts": 1} for i in range(10)]
    client.post("/sdk/events", json={"events": ev}, headers={"authorization": "sdk-prod-demo"})
    r = client.get("/api/experiments/exp-b").json()
    assert r["primary_metric"] == "buy" and [v["users"] for v in r["variations"]] == [5, 5]
    assert client.get("/api/experiments").json()["experiments"][0]["key"] == "exp-b"
