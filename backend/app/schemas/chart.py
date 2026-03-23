"""Chart data models for BI agent chart emission."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ChartAxis(BaseModel):
    label: str
    key: str
    color: str | None = None


class ChartOptions(BaseModel):
    stacked: bool = False
    show_legend: bool = True
    show_values: bool = False
    percentage_mode: bool = False
    sort_by: str | None = None
    orientation: Literal["vertical", "horizontal"] = "vertical"


class ChartData(BaseModel):
    chart_type: Literal["bar", "line", "pie", "area", "scatter", "donut", "histogram"] = "bar"
    title: str
    subtitle: str | None = None
    x_axis: ChartAxis
    y_axes: list[ChartAxis]
    data: list[dict]
    options: ChartOptions | None = None
