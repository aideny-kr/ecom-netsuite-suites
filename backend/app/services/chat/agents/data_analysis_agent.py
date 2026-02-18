"""Data analysis specialist agent.

Interprets raw query results — computes aggregations, spots trends,
compares periods, formats tables. Does NOT execute queries or search docs;
it works purely with data provided by other agents.
"""

from __future__ import annotations

import uuid

from app.services.chat.agents.base_agent import BaseSpecialistAgent


class DataAnalysisAgent(BaseSpecialistAgent):
    """Specialist agent for data interpretation and analysis."""

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        super().__init__(tenant_id, user_id, correlation_id)

    @property
    def agent_name(self) -> str:
        return "analysis"

    @property
    def max_steps(self) -> int:
        return 1  # Pure reasoning — single call

    @property
    def system_prompt(self) -> str:
        return (
            "You are a data analysis specialist. You receive raw data from query results "
            "and your job is to interpret, analyse, and present it clearly.\n"
            "\n"
            "YOUR CAPABILITIES:\n"
            "- Compute totals, averages, min/max, percentages, growth rates\n"
            "- Compare data across periods (month-over-month, year-over-year)\n"
            "- Identify trends, outliers, and anomalies\n"
            "- Format results in clean markdown tables\n"
            "- Provide business insights and observations\n"
            "\n"
            "RULES:\n"
            "- Work ONLY with the data provided to you. Do NOT fabricate numbers.\n"
            "- If the data is insufficient for the requested analysis, say so clearly.\n"
            "- Present numbers with appropriate formatting (commas, currency symbols, etc.)\n"
            "- When presenting tables, use markdown table format.\n"
            "- Keep your analysis concise and focused on what was asked."
        )

    @property
    def tool_definitions(self) -> list[dict]:
        return []  # No tools — pure reasoning agent
