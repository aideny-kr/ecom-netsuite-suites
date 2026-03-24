"""Tests for Tier 1 rule-based routing."""

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig, RoutingRule
from app.services.chat.routing.rule_router import RuleRouter


def _make_agent(agent_id: str, patterns: list[tuple[str, int]], enabled: bool = True) -> tuple[AgentYAMLConfig, bool]:
    rules = [RoutingRule(pattern=p, priority=pri) for p, pri in patterns]
    config = AgentYAMLConfig(
        agent_id=agent_id,
        display_name=agent_id.replace("-", " ").title(),
        description=f"Agent for {agent_id}",
        routing_rules=rules,
    )
    return config, enabled


class TestRuleRouter:
    def test_single_match_returns_agent_id(self):
        agents = [_make_agent("pricing-agent", [("(?i)(price|pricing|margin)", 0)])]
        router = RuleRouter(agents)
        assert router.route("what's the price for SKU-1234") == "pricing-agent"

    def test_no_match_returns_none(self):
        agents = [_make_agent("pricing-agent", [("(?i)(price|pricing)", 0)])]
        router = RuleRouter(agents)
        assert router.route("hello how are you") is None

    def test_ambiguous_match_returns_none(self):
        agents = [
            _make_agent("pricing-agent", [("(?i)price", 0)]),
            _make_agent("recon-agent", [("(?i)reconcil", 0)]),
        ]
        router = RuleRouter(agents)
        # "price reconciliation" matches both — ambiguous
        assert router.route("price reconciliation report") is None

    def test_case_insensitive_matching(self):
        agents = [_make_agent("pricing-agent", [("(?i)price", 0)])]
        router = RuleRouter(agents)
        assert router.route("WHAT IS THE PRICE") == "pricing-agent"

    def test_priority_breaks_tie(self):
        agents = [
            _make_agent("low-pri", [("(?i)price", 0)]),
            _make_agent("high-pri", [("(?i)price", 10)]),
        ]
        router = RuleRouter(agents)
        assert router.route("price check") == "high-pri"

    def test_empty_agents_returns_none(self):
        router = RuleRouter([])
        assert router.route("anything") is None

    def test_disabled_agent_excluded(self):
        agents = [_make_agent("pricing-agent", [("(?i)price", 0)], enabled=False)]
        router = RuleRouter(agents)
        assert router.route("what's the price") is None

    def test_multiple_patterns_per_agent(self):
        agents = [
            _make_agent(
                "pricing-agent",
                [
                    ("(?i)price", 0),
                    ("(?i)margin", 0),
                    ("(?i)cost", 0),
                ],
            )
        ]
        router = RuleRouter(agents)
        assert router.route("what's the margin") == "pricing-agent"
