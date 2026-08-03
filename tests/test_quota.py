"""Quota split and redistribution (CLAUDE.md section 5).

Pure logic, no database.
"""

from __future__ import annotations

import pytest

from app.core.constants import RESULT_LIMIT_OPTIONS
from app.services.quota import compute_quotas, redistribute


def test_20_across_2_portals_is_10_each() -> None:
    assert compute_quotas(20, 2) == [10, 10]


def test_50_across_4_portals_is_12_or_13_each() -> None:
    quotas = compute_quotas(50, 4)

    assert sum(quotas) == 50
    assert set(quotas) == {12, 13}


def test_100_across_8_portals_is_12_or_13_each() -> None:
    quotas = compute_quotas(100, 8)

    assert sum(quotas) == 100
    assert set(quotas) == {12, 13}


@pytest.mark.parametrize("limit", RESULT_LIMIT_OPTIONS)
@pytest.mark.parametrize("portal_count", range(1, 9))
def test_quotas_always_sum_to_the_requested_limit(
    limit: int, portal_count: int
) -> None:
    quotas = compute_quotas(limit, portal_count)

    assert sum(quotas) == limit
    assert len(quotas) == portal_count


@pytest.mark.parametrize("limit", RESULT_LIMIT_OPTIONS)
@pytest.mark.parametrize("portal_count", range(1, 9))
def test_no_portal_gets_more_than_one_extra_slot(
    limit: int, portal_count: int
) -> None:
    # "Approximately equal" means a spread of at most one.
    quotas = compute_quotas(limit, portal_count)

    assert max(quotas) - min(quotas) <= 1


def test_the_remainder_goes_to_the_first_portals() -> None:
    # Deterministic, so creating the same run twice produces the same quotas.
    assert compute_quotas(20, 3) == [7, 7, 6]


def test_a_portal_count_of_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        compute_quotas(20, 0)


# --- Redistribution ---------------------------------------------------------


def test_unused_slots_move_to_portals_with_more_qualified_jobs() -> None:
    # Portal B found only 3 qualified jobs against a quota of 10; A has plenty.
    result = redistribute([10, 10], [40, 3])

    assert result.final_quotas == [17, 3]
    assert result.redistributed == 7
    assert result.shortfalls == [0, 7]


def test_redistribution_never_exceeds_what_actually_qualified() -> None:
    # Nobody has spare qualified jobs, so the run simply returns fewer.
    result = redistribute([10, 10], [4, 3])

    assert result.final_quotas == [4, 3]
    assert result.redistributed == 0
    assert sum(result.shortfalls) == 13


def test_spare_slots_are_spread_rather_than_dumped_on_one_portal() -> None:
    result = redistribute([10, 10, 10], [12, 12, 2])

    # Portal C is 8 short; A and B each have 2 spare qualified jobs.
    assert result.final_quotas == [12, 12, 2]
    assert result.redistributed == 4
    assert result.shortfalls == [0, 0, 8]


def test_nothing_moves_when_every_portal_fills_its_quota() -> None:
    result = redistribute([13, 13, 12, 12], [20, 20, 20, 20])

    assert result.final_quotas == [13, 13, 12, 12]
    assert result.redistributed == 0
    assert result.shortfalls == [0, 0, 0, 0]


def test_mismatched_input_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        redistribute([10, 10], [5])
