"""Third-party analytics queries (Heap, Segment, Mixpanel, Amplitude,
Firebase, GA) land in BigQuery as upstream tables. The bi-agent's
Tier 1 regex was too narrow — 'analyze Heap pageview funnel' missed
every pattern and fell through to unified-agent."""

import re
from pathlib import Path

import yaml

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs" / "bi_agent.yaml"
)


def _load_patterns() -> list[str]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    return [rule["pattern"] for rule in cfg.get("routing_rules", [])]


class TestBiAgentRouting:
    def test_heap_pageview_query_matches(self):
        patterns = _load_patterns()
        query = "analyze Heap pageview funnel for AMD Ryzen SKUs"
        assert any(re.search(p, query) for p in patterns), "bi-agent's Tier 1 regex must match Heap/analytics queries."

    def test_funnel_conversion_matches(self):
        patterns = _load_patterns()
        query = "what's the funnel conversion rate for checkout?"
        assert any(re.search(p, query) for p in patterns)

    def test_attribution_matches(self):
        patterns = _load_patterns()
        query = "show marketing attribution by channel"
        assert any(re.search(p, query) for p in patterns)

    def test_mixpanel_matches(self):
        patterns = _load_patterns()
        query = "pull the last 30 days of mixpanel events"
        assert any(re.search(p, query) for p in patterns)
