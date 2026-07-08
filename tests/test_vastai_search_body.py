"""Unit tests for VastAIProvider.build_search_body.

Regression guard for the GB-vs-GiB VRAM filter bug: GPUs marketed as "80GB"
report slightly under 80 GiB on Vast (e.g. H100 SXM = 81559 MiB = 79.6 GiB), so
a strict ``gpu_ram_min * 1024`` threshold silently excluded every offer.
"""

from __future__ import annotations

from beam.config import VastAIMachineConfig
from beam.providers.vastai import GPU_RAM_TOLERANCE, VastAIProvider

# Observed Vast ``gpu_ram`` values (MiB) for 80GB-class cards.
H100_SXM_80GB_MIB = 81559  # 79.6 GiB — reports *under* a clean 80 GiB boundary
A100_SXM4_80GB_MIB = 81920  # 80.0 GiB exactly
A100_SXM4_40GB_MIB = 40960  # 40.0 GiB


def _threshold(gpu_ram_min: float) -> int:
    body = VastAIProvider.build_search_body(
        VastAIMachineConfig(gpu_ram_min=gpu_ram_min)
    )
    return body["gpu_ram"]["gte"]


def test_80gb_min_admits_h100_reporting_under_80_gib():
    """gpu_ram_min=80 must not exclude an H100 SXM 80GB (reports 79.6 GiB)."""
    assert _threshold(80) <= H100_SXM_80GB_MIB
    assert _threshold(80) <= A100_SXM4_80GB_MIB


def test_80gb_min_still_excludes_40gb_cards():
    """The tolerance must not blur into the adjacent (40GB) VRAM tier."""
    assert _threshold(80) > A100_SXM4_40GB_MIB


def test_threshold_applies_configured_tolerance():
    assert _threshold(80) == int(80 * 1024 * GPU_RAM_TOLERANCE)


def test_no_gpu_ram_filter_when_unset():
    body = VastAIProvider.build_search_body(VastAIMachineConfig())
    assert "gpu_ram" not in body


# --- gpu_name whole-token prefix matching ----------------------------------

RTX_PRO_6000_LABELS = ["RTX PRO 6000 S", "RTX PRO 6000 WS"]


def test_partial_gpu_name_matches_all_variants():
    m = VastAIProvider._gpu_name_matches
    assert all(m(label, "RTX PRO 6000") for label in RTX_PRO_6000_LABELS)


def test_qualified_gpu_name_matches_only_itself():
    m = VastAIProvider._gpu_name_matches
    assert m("RTX PRO 6000 S", "RTX PRO 6000 S")
    assert not m("RTX PRO 6000 WS", "RTX PRO 6000 S")


def test_prefix_does_not_cross_token_boundary():
    """'RTX 4080' must not match 'RTX 4080S' (partial token)."""
    m = VastAIProvider._gpu_name_matches
    assert not m("RTX 4080S", "RTX 4080")
    assert m("RTX 4080", "RTX 4080")


def test_build_body_uses_in_filter_for_resolved_names():
    body = VastAIProvider.build_search_body(
        VastAIMachineConfig(gpu_name="RTX PRO 6000"), gpu_names=RTX_PRO_6000_LABELS
    )
    assert body["gpu_name"] == {"in": RTX_PRO_6000_LABELS}


def test_build_body_falls_back_to_eq_without_resolved_names():
    body = VastAIProvider.build_search_body(VastAIMachineConfig(gpu_name="H100 SXM"))
    assert body["gpu_name"] == {"eq": "H100 SXM"}
