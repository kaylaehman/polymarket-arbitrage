"""Tests for the Phase 3 AI Signals dashboard plumbing."""

from dashboard.server import DashboardState, get_embedded_html


def test_add_ai_signal_and_serialize():
    st = DashboardState()
    assert st.to_dict()["ai_signals"] == []

    st.add_ai_signal({"market": "m1", "direction": "bullish", "confidence": 0.8, "reason": "news"})
    out = st.to_dict()["ai_signals"]
    assert len(out) == 1
    assert out[0]["direction"] == "bullish"
    assert "timestamp" in out[0]  # stamped on insert


def test_ai_signals_capped():
    st = DashboardState()
    for i in range(250):
        st.add_ai_signal({"market": f"m{i}", "direction": "agree", "confidence": 0.5})
    # internal list is trimmed, and to_dict only exposes the last 50
    assert len(st.ai_signals) <= 200
    assert len(st.to_dict()["ai_signals"]) == 50


def test_embedded_html_has_ai_panel_and_renderer():
    html = get_embedded_html()
    # The card, its mount point, and the JS renderer + dispatch must all exist.
    assert 'id="aiSignalList"' in html
    assert "function updateAiSignals()" in html
    assert "updateAiSignals();" in html
