"use client";

import React, { useState } from "react";
import {
  BarChart,
  Bar,
  ComposedChart,
  Line,
  PieChart,
  Pie,
  Cell,
  AreaChart,
  Area,
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import type { ChartData } from "@/lib/types";

// ── AI-native color palette ──────────────────────────────────────────────────

const CHART_COLORS = [
  "#6366f1", // indigo
  "#8b5cf6", // violet
  "#06b6d4", // cyan
  "#f59e0b", // amber
  "#10b981", // emerald
  "#ec4899", // pink
  "#f97316", // orange
  "#14b8a6", // teal
];

// ── Smart number formatting ──────────────────────────────────────────────────

function formatValue(value: number, label?: string): string {
  const isCurrency =
    /revenue|amount|cost|price|sales|profit|spend|budget|total_revenue|netamount/i.test(
      label || "",
    );

  const abs = Math.abs(value);

  if (abs >= 1_000_000_000) {
    const formatted = (value / 1_000_000_000).toFixed(1).replace(/\.0$/, "");
    return isCurrency ? `$${formatted}B` : `${formatted}B`;
  }
  if (abs >= 1_000_000) {
    const formatted = (value / 1_000_000).toFixed(1).replace(/\.0$/, "");
    return isCurrency ? `$${formatted}M` : `${formatted}M`;
  }
  if (abs >= 1_000) {
    const formatted = (value / 1_000).toFixed(1).replace(/\.0$/, "");
    return isCurrency ? `$${formatted}K` : `${formatted}K`;
  }

  return isCurrency ? `$${value.toLocaleString()}` : value.toLocaleString();
}

function formatTooltipValue(value: number, label?: string): string {
  const isCurrency =
    /revenue|amount|cost|price|sales|profit|spend|budget|total_revenue|netamount/i.test(
      label || "",
    );

  if (isCurrency) {
    return `$${value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
  }
  return value.toLocaleString();
}

// ── Custom tooltip ───────────────────────────────────────────────────────────

function CustomTooltip({ active, payload, label, yAxes }: any) {
  if (!active || !payload?.length) return null;

  return (
    <div className="rounded-xl border border-white/[0.08] bg-[hsl(240,10%,10%)]/95 backdrop-blur-md px-4 py-3 shadow-[0_8px_32px_rgba(0,0,0,0.4)]">
      <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">
        {label}
      </p>
      {payload.map((entry: any, i: number) => (
        <div key={i} className="flex items-center gap-2 text-[13px]">
          <div
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="text-muted-foreground">{entry.name}:</span>
          <span className="font-semibold text-foreground">
            {formatTooltipValue(
              entry.value,
              yAxes?.find((y: any) => y.key === entry.dataKey)?.label,
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Shared axis/grid props ───────────────────────────────────────────────────

function SharedGrid() {
  return (
    <CartesianGrid
      strokeDasharray="none"
      stroke="hsl(240, 5%, 20%)"
      strokeOpacity={0.4}
      vertical={false}
    />
  );
}

function SharedXAxis({ dataKey }: { dataKey: string }) {
  return (
    <XAxis
      dataKey={dataKey}
      tick={{ fontSize: 11, fill: "hsl(240, 5%, 55%)" }}
      axisLine={{ stroke: "hsl(240, 5%, 20%)" }}
      tickLine={false}
      dy={8}
    />
  );
}

function SharedYAxis({ label }: { label?: string }) {
  return (
    <YAxis
      tick={{ fontSize: 11, fill: "hsl(240, 5%, 55%)" }}
      axisLine={false}
      tickLine={false}
      tickFormatter={(value) => formatValue(value, label)}
      width={65}
      dx={-5}
    />
  );
}

function SharedLegend() {
  return (
    <Legend
      wrapperStyle={{ paddingTop: "16px" }}
      iconType="circle"
      iconSize={8}
      formatter={(value: string) => (
        <span
          style={{
            color: "hsl(240, 5%, 65%)",
            fontSize: "11px",
            marginLeft: "4px",
          }}
        >
          {value}
        </span>
      )}
    />
  );
}

// ── Main component ───────────────────────────────────────────────────────────

export function ChartRenderer({ data }: { data: ChartData }) {
  const [showData, setShowData] = useState(false);

  if (!data.data || data.data.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">No data to display</div>
    );
  }

  const tooltipProps = {
    content: <CustomTooltip yAxes={data.y_axes} />,
    cursor: { fill: "hsl(240, 5%, 15%)", fillOpacity: 0.5 },
  };

  const renderChart = () => {
    switch (data.chart_type) {
      case "line":
        return (
          <ComposedChart data={data.data}>
            <defs>
              {data.y_axes.map((y, i) => (
                <linearGradient
                  key={`lineGrad-${i}`}
                  id={`lineGrad-${i}`}
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="1"
                >
                  <stop
                    offset="0%"
                    stopColor={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                    stopOpacity={0.15}
                  />
                  <stop
                    offset="100%"
                    stopColor={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                    stopOpacity={0}
                  />
                </linearGradient>
              ))}
            </defs>
            <SharedGrid />
            <SharedXAxis dataKey={data.x_axis.key} />
            <SharedYAxis label={data.y_axes[0]?.label} />
            <Tooltip {...tooltipProps} />
            {data.options?.show_legend !== false && <SharedLegend />}
            {data.y_axes.map((y, i) => (
              <React.Fragment key={y.key}>
                <Area
                  type="monotone"
                  dataKey={y.key}
                  fill={`url(#lineGrad-${i})`}
                  stroke="none"
                />
                <Line
                  type="monotone"
                  dataKey={y.key}
                  stroke={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                  strokeWidth={2.5}
                  dot={{
                    r: 3,
                    fill: "hsl(240, 10%, 8%)",
                    strokeWidth: 2,
                    stroke:
                      y.color || CHART_COLORS[i % CHART_COLORS.length],
                  }}
                  activeDot={{
                    r: 6,
                    fill:
                      y.color || CHART_COLORS[i % CHART_COLORS.length],
                    stroke: "hsl(240, 10%, 8%)",
                    strokeWidth: 2,
                  }}
                  name={y.label}
                  animationDuration={1000}
                  animationEasing="ease-out"
                />
              </React.Fragment>
            ))}
          </ComposedChart>
        );
      case "pie":
      case "donut":
        return (
          <PieChart>
            <Pie
              data={data.data}
              dataKey={data.y_axes[0]?.key || "value"}
              nameKey={data.x_axis.key}
              cx="50%"
              cy="50%"
              innerRadius={data.chart_type === "donut" ? "55%" : 0}
              outerRadius="80%"
              paddingAngle={2}
              strokeWidth={0}
              animationDuration={1000}
              label={({ name, value }) =>
                `${name}: ${formatValue(value, data.y_axes[0]?.label)}`
              }
            >
              {data.data.map((_, i) => (
                <Cell
                  key={i}
                  fill={CHART_COLORS[i % CHART_COLORS.length]}
                  fillOpacity={0.9}
                />
              ))}
            </Pie>
            <Tooltip {...tooltipProps} />
            <SharedLegend />
          </PieChart>
        );
      case "area":
        return (
          <AreaChart data={data.data}>
            <defs>
              {data.y_axes.map((y, i) => (
                <linearGradient
                  key={`chartGradient-${i}`}
                  id={`chartGradient-${i}`}
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="1"
                >
                  <stop
                    offset="0%"
                    stopColor={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                    stopOpacity={0.3}
                  />
                  <stop
                    offset="100%"
                    stopColor={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                    stopOpacity={0.02}
                  />
                </linearGradient>
              ))}
            </defs>
            <SharedGrid />
            <SharedXAxis dataKey={data.x_axis.key} />
            <SharedYAxis label={data.y_axes[0]?.label} />
            <Tooltip {...tooltipProps} />
            {data.options?.show_legend !== false && <SharedLegend />}
            {data.y_axes.map((y, i) => (
              <Area
                key={y.key}
                type="monotone"
                dataKey={y.key}
                fill={`url(#chartGradient-${i})`}
                stroke={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                strokeWidth={2}
                fillOpacity={1}
                name={y.label}
                animationDuration={1000}
              />
            ))}
          </AreaChart>
        );
      case "scatter":
        return (
          <ScatterChart>
            <SharedGrid />
            <SharedXAxis dataKey={data.x_axis.key} />
            <SharedYAxis label={data.y_axes[0]?.label} />
            <Tooltip {...tooltipProps} />
            <Scatter
              data={data.data}
              fill={CHART_COLORS[0]}
              animationDuration={800}
            />
          </ScatterChart>
        );
      default: // bar, histogram
        return (
          <BarChart data={data.data}>
            <SharedGrid />
            <SharedXAxis dataKey={data.x_axis.key} />
            <SharedYAxis label={data.y_axes[0]?.label} />
            <Tooltip {...tooltipProps} />
            {data.options?.show_legend !== false && <SharedLegend />}
            {data.y_axes.map((y, i) => (
              <Bar
                key={y.key}
                dataKey={y.key}
                fill={y.color || CHART_COLORS[i % CHART_COLORS.length]}
                name={y.label}
                radius={[4, 4, 0, 0]}
                animationDuration={800}
                animationEasing="ease-out"
                fillOpacity={0.85}
                activeBar={{
                  fillOpacity: 1,
                  stroke: y.color || CHART_COLORS[i % CHART_COLORS.length],
                  strokeWidth: 1,
                }}
                stackId={data.options?.stacked ? "stack" : undefined}
              />
            ))}
          </BarChart>
        );
    }
  };

  return (
    <div className="my-4 rounded-2xl border border-white/[0.06] bg-[hsl(240,10%,8%)]/80 backdrop-blur-sm p-5 shadow-[0_4px_24px_rgba(0,0,0,0.3)]">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="h-4 w-1 rounded-full bg-indigo-500" />
          <div>
            <h4 className="text-[14px] font-semibold text-foreground">
              {data.title}
            </h4>
            {data.subtitle && (
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {data.subtitle}
              </p>
            )}
          </div>
        </div>
        <button
          onClick={() => setShowData(!showData)}
          className="rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-white/[0.05] hover:text-foreground"
        >
          {showData ? "View Chart" : "View Data"}
        </button>
      </div>

      {!showData ? (
        <ResponsiveContainer width="100%" height={340}>
          {renderChart()}
        </ResponsiveContainer>
      ) : (
        <div className="max-h-[340px] overflow-auto rounded-lg border border-white/[0.06]">
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 bg-[hsl(240,10%,10%)]">
              <tr>
                <th className="border-b border-white/[0.06] p-2 text-left text-[11px] font-medium text-muted-foreground">
                  {data.x_axis.label}
                </th>
                {data.y_axes.map((y) => (
                  <th
                    key={y.key}
                    className="border-b border-white/[0.06] p-2 text-right text-[11px] font-medium text-muted-foreground"
                  >
                    {y.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.data.map((row, i) => (
                <tr
                  key={i}
                  className="border-b border-white/[0.03] transition-colors hover:bg-white/[0.02]"
                >
                  <td className="p-2 text-foreground">
                    {String(row[data.x_axis.key] ?? "")}
                  </td>
                  {data.y_axes.map((y) => (
                    <td
                      key={y.key}
                      className="p-2 text-right font-mono text-foreground"
                    >
                      {typeof row[y.key] === "number"
                        ? formatTooltipValue(row[y.key] as number, y.label)
                        : String(row[y.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
