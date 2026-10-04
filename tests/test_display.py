"""Terminal rendering helpers."""

from runspool.display import truncate_by_width


def test_truncate_counts_wide_characters_as_two_columns():
    assert truncate_by_width("short", 10) == "short"
    assert truncate_by_width("巨头都在抢AI助理的入口", 10) == "巨头都在抢..."
    assert truncate_by_width("ab巨头", 5) == "ab巨..."  # 头 would straddle the limit
    assert truncate_by_width("巨头ab", 6) == "巨头ab"
