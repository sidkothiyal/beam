"""Unit tests for the self-competition guard.

Running `beam run` twice with ``pick_strategy=best_value`` deterministically
ranks the same offer first, so the second run would bid on the machine the
first run already occupies — outbidding itself. The guard excludes offers on
machines the account already has a live instance on.
"""

from __future__ import annotations

from beam.providers.vastai import VastAIProvider


def _provider() -> VastAIProvider:
    return VastAIProvider(api_key="test-key")


# --- _filter_excluded_machines ---------------------------------------------

OFFERS = [
    {"id": "1", "machine_id": 100},
    {"id": "2", "machine_id": 200},
    {"id": "3", "machine_id": 300},
]


def test_filter_drops_offers_on_excluded_machines():
    kept = VastAIProvider._filter_excluded_machines(OFFERS, {"200"})
    assert [o["id"] for o in kept] == ["1", "3"]


def test_filter_matches_across_int_str_machine_id_types():
    # Offers report machine_id as int; the exclude set holds strings.
    kept = VastAIProvider._filter_excluded_machines(OFFERS, {"100", "300"})
    assert [o["id"] for o in kept] == ["2"]


def test_filter_noop_when_exclude_empty_or_none():
    assert VastAIProvider._filter_excluded_machines(OFFERS, None) == OFFERS
    assert VastAIProvider._filter_excluded_machines(OFFERS, set()) == OFFERS


# --- owned_machine_ids -----------------------------------------------------


def test_owned_machine_ids_collects_live_instances(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, "list_instances", lambda: [
        {"id": "a", "machine_id": 100, "cur_state": "running"},
        {"id": "b", "machine_id": 200, "cur_state": "loading"},
    ])
    assert p.owned_machine_ids() == {"100", "200"}


def test_owned_machine_ids_ignores_released_and_blank(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, "list_instances", lambda: [
        {"id": "a", "machine_id": 100, "cur_state": "running"},
        {"id": "b", "machine_id": 200, "cur_state": "exited"},   # released host
        {"id": "c", "machine_id": None, "cur_state": "running"},  # no machine
        {"id": "d", "cur_state": "running"},                      # missing key
    ])
    assert p.owned_machine_ids() == {"100"}


def test_owned_machine_ids_empty_on_api_error(monkeypatch):
    from beam.providers.base import ProviderError

    p = _provider()

    def _boom():
        raise ProviderError("boom")

    monkeypatch.setattr(p, "list_instances", _boom)
    # Must not raise — a transient API error should never block provisioning.
    assert p.owned_machine_ids() == set()


# --- resolve_offer_id integration ------------------------------------------


def test_resolve_offer_id_skips_owned_machine(monkeypatch):
    from beam.config import VastAIMachineConfig

    p = _provider()
    # Two offers; the "best value" one lives on a machine we already occupy.
    monkeypatch.setattr(p, "_resolve_gpu_names", lambda cfg: None)
    monkeypatch.setattr(p, "search_offers", lambda body: [
        {"id": "10", "machine_id": 100, "min_bid": 0.10,
         "dlperf_per_dphtotal": 99.0, "num_gpus": 1, "gpu_ram": 81920},
        {"id": "20", "machine_id": 200, "min_bid": 0.12,
         "dlperf_per_dphtotal": 50.0, "num_gpus": 1, "gpu_ram": 81920},
    ])

    cfg = VastAIMachineConfig(pick_strategy="best_value", offer_type="bid")
    resolved = p.resolve_offer_id(cfg, exclude_machine_ids={"100"})
    # Offer 10 is the best value but is on an occupied host, so 20 wins.
    assert resolved.offer_id == "20"
