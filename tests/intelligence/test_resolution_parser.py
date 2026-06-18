"""Tests for intelligence.resolution_parser and FEAT-02 prompt threading."""

from intelligence.ai_analyzer import AIAnalyzer
from intelligence.resolution_parser import ResolutionParser


def test_empty_description_returns_none():
    parser = ResolutionParser()
    assert parser.extract(None) is None
    assert parser.extract("") is None
    assert parser.extract("   ") is None


def test_short_description_passes_through_cleaned():
    parser = ResolutionParser()
    out = parser.extract("Resolves YES if the Fed raises rates  in   June 2026.")
    # Whitespace collapsed, content preserved.
    assert out == "Resolves YES if the Fed raises rates in June 2026."


def test_long_description_prefers_resolution_sentences():
    parser = ResolutionParser(max_chars=120)
    filler = "This market is about monetary policy. " * 5  # no keywords
    criteria = "It resolves YES only if the official Fed press release confirms a hike."
    out = parser.extract(filler + criteria)
    assert out is not None
    assert "resolves YES" in out
    assert len(out) <= 120


def test_long_description_without_keywords_truncates():
    parser = ResolutionParser(max_chars=50)
    out = parser.extract("x" * 200)
    assert out.endswith("…")
    assert len(out) <= 51  # 50 chars + ellipsis


def test_prompt_includes_resolution_criteria():
    prompt = AIAnalyzer._build_user_prompt(
        market_question="Will the Fed raise rates?",
        current_yes_price=0.6,
        articles=[],
        lookback_hours=4,
        resolution_criteria="Resolves YES per the June FOMC press release.",
    )
    assert "Resolution criteria: Resolves YES per the June FOMC press release." in prompt
    assert "Given this specific resolution criteria" in prompt


def test_prompt_omits_criteria_block_when_absent():
    prompt = AIAnalyzer._build_user_prompt(
        market_question="Will the Fed raise rates?",
        current_yes_price=0.6,
        articles=[],
        lookback_hours=4,
        resolution_criteria=None,
    )
    assert "Resolution criteria:" not in prompt
    assert "Based on this news, what is the true probability for YES?" in prompt
