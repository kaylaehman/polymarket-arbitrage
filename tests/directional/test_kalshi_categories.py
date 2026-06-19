import pytest
from utils.kalshi_categories import categorize


def test_known_prefixes_map_to_categories():
    # NFLGAME is in SUBCATEGORY_PATTERNS — "Sports"
    assert categorize("KXNFLGAME-26SEP-KC") == "Sports"
    # "KXSENATE-26NOV-R": "SENATE" is a pattern in SUBCATEGORY_PATTERNS -> "Politics"
    assert categorize("KXSENATE-26NOV-R") == "Politics"
    # Unknown ticker returns "Other"
    assert categorize("KXUNKNOWNXYZ-99") == "Other"
