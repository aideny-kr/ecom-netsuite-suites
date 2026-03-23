"use client";

import { useState } from "react";
import {
  BarChart,
  Bar,
  LineChart,
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

const COLORS = [
  "#3b82f6",
  "#ef4444",
  "#22c55e",
  "#f59e0b",
  "#8b5cf6",
  "#ec4899",
  "#06b6d4",
  "#f97316",
];

export function ChartRenderer({ data }: { data: ChartData }) {
  const [showData, setShowData] = useState(false);

  if (!data.data || data.data.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">No data to display</div>
    );
  }

  const renderChart = () => {
    switch (data.chart_type) {
      case "line":
        return (
          <LineChart data={data.data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey={data.x_axis.key} />
            <YAxis />
            <Tooltip />
            {data.options?.show_legend !== false && <Legend />}
            {data.y_axes.map((y, i) => (
              <Line
                key={y.key}
                type="monotone"
                dataKey={y.key}
                stroke={y.color || COLORS[i % COLORS.length]}
                name={y.label}
              />
            ))}
          </LineChart>
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
              innerRadius={data.chart_type === "donut" ? 60 : 0}
              outerRadius={80}
              label
            >
              {data.data.map((_, i) => (
                <Cell key={i} fill={COLORS[i % COLORS.length]} />
              ))}
            </Pie>
            <Tooltip />
            <Legend />
          </PieChart>
        );
      case "area":
        return (
          <AreaChart data={data.data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey={data.x_axis.key} />
            <YAxis />
            <Tooltip />
            {data.options?.show_legend !== false && <Legend />}
            {data.y_axes.map((y, i) => (
              <Area
                key={y.key}
                type="monotone"
                dataKey={y.key}
                fill={y.color || COLORS[i % COLORS.length]}
                stroke={y.color || COLORS[i % COLORS.length]}
                fillOpacity={0.3}
                name={y.label}
              />
            ))}
          </AreaChart>
        );
      case "scatter":
        return (
          <ScatterChart>
            <CartesianGrid />
            <XAxis dataKey={data.x_axis.key} name={data.x_axis.label} />
            <YAxis
              dataKey={data.y_axes[0]?.key}
              name={data.y_axes[0]?.label}
            />
            <Tooltip cursor={{ strokeDasharray: "3 3" }} />
            <Scatter data={data.data} fill={COLORS[0]} />
          </ScatterChart>
        );
      default: // bar, histogram
        return (
          <BarChart data={data.data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey={data.x_axis.key} />
            <YAxis />
            <Tooltip />
            {data.options?.show_legend !== false && <Legend />}
            {data.y_axes.map((y, i) => (
              <Bar
                key={y.key}
                dataKey={y.key}
                fill={y.color || COLORS[i % COLORS.length]}
                name={y.label}
                stackId={data.options?.stacked ? "stack" : undefined}
              />
            ))}
          </BarChart>
        );
    }
  };

  return (
    <div className="my-3 rounded-xl border bg-card p-4 shadow-soft">
      <div className="mb-2 flex items-center justify-between">
        <div>
          <h4 className="text-[14px] font-semibold">{data.title}</h4>
          {data.subtitle && (
            <p className="text-[12px] text-muted-foreground">
              {data.subtitle}
            </p>
          )}
        </div>
        <button
          onClick={() => setShowData(!showData)}
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          {showData ? "Hide Data" : "View Data"}
        </button>
      </div>

      {!showData ? (
        <ResponsiveContainer width="100%" height={300}>
          {renderChart()}
        </ResponsiveContainer>
      ) : (
        <div className="max-h-[300px] overflow-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr>
                <th className="text-left p-1">{data.x_axis.label}</th>
                {data.y_axes.map((y) => (
                  <th key={y.key} className="text-right p-1">
                    {y.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.data.map((row, i) => (
                <tr key={i} className="border-t">
                  <td className="p-1">
                    {String(row[data.x_axis.key] ?? "")}
                  </td>
                  {data.y_axes.map((y) => (
                    <td key={y.key} className="text-right p-1">
                      {String(row[y.key] ?? "")}
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
