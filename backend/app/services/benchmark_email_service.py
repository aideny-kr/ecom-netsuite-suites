"""Email notifications for vs-MCP benchmark results.

Sends two types of emails:
  1. Daily digest — after every nightly run, summarizes wins/losses/ties
  2. Regression alert — loud subject + red formatting when delta drops

Configuration via env vars:
  BENCHMARK_ALERT_EMAIL_TO    — recipient(s), comma-separated
  BENCHMARK_ALERT_EMAIL_FROM  — sender address (e.g. alerts@suitestudio.ai)
  BENCHMARK_SMTP_HOST         — SMTP server (default: smtp.gmail.com)
  BENCHMARK_SMTP_PORT         — SMTP port (default: 587)
  BENCHMARK_SMTP_USER         — SMTP username
  BENCHMARK_SMTP_PASSWORD     — SMTP password or app password

If any required env var is missing, emails are silently skipped (logged
at WARNING) so the nightly task never crashes over email configuration.
"""

# ruff: noqa: E501 — HTML templates have long lines by nature
from __future__ import annotations

import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

logger = structlog.get_logger()


def _get_config() -> dict | None:
    """Read SMTP config from env. Returns None if not configured."""
    to = os.environ.get("BENCHMARK_ALERT_EMAIL_TO")
    if not to:
        return None
    return {
        "to": [addr.strip() for addr in to.split(",") if addr.strip()],
        "from": os.environ.get("BENCHMARK_ALERT_EMAIL_FROM", "benchmark@suitestudio.ai"),
        "host": os.environ.get("BENCHMARK_SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("BENCHMARK_SMTP_PORT", "587")),
        "user": os.environ.get("BENCHMARK_SMTP_USER", ""),
        "password": os.environ.get("BENCHMARK_SMTP_PASSWORD", ""),
    }


def send_benchmark_digest(
    *,
    run_date: date,
    stats: dict,
    regression_detected: bool = False,
) -> bool:
    """Send a benchmark digest email. Returns True if sent, False if skipped/failed."""
    config = _get_config()
    if not config:
        logger.warning("benchmark_email.not_configured", reason="BENCHMARK_ALERT_EMAIL_TO not set")
        return False

    # Build subject
    ours_wins = stats.get("ours_wins", 0)
    mcp_wins = stats.get("mcp_wins", 0)
    ties = stats.get("ties", 0)
    total = stats.get("cases_total", 0) or stats.get("cases_run", 0)
    avg_delta = stats.get("avg_delta_accuracy", 0.0)

    if regression_detected:
        subject = f"⚠️ REGRESSION — Agent Benchmark {run_date}: MCP wins {mcp_wins}/{total} cases (Δ {avg_delta:+.2f})"
    elif mcp_wins > 0:
        subject = f"Agent Benchmark {run_date}: {ours_wins} wins, {mcp_wins} losses, {ties} ties (Δ {avg_delta:+.2f})"
    else:
        subject = f"✅ Agent Benchmark {run_date}: {ours_wins} wins, {ties} ties — beating MCP (Δ {avg_delta:+.2f})"

    # Build HTML body
    body = _build_html_body(
        run_date=run_date,
        stats=stats,
        regression_detected=regression_detected,
    )

    return _send_email(config, subject, body)


def _build_html_body(*, run_date: date, stats: dict, regression_detected: bool) -> str:
    ours_wins = stats.get("ours_wins", 0)
    mcp_wins = stats.get("mcp_wins", 0)
    ties = stats.get("ties", 0)
    failures = stats.get("failures", 0)
    total = stats.get("cases_total", 0) or stats.get("cases_run", 0)
    avg_delta = stats.get("avg_delta_accuracy", 0.0)
    yesterday_delta = stats.get("yesterday_delta")
    latency_breaches = stats.get("latency_breaches", 0)
    latency_cases = stats.get("latency_breach_cases", [])

    # Colors
    delta_color = "#22c55e" if avg_delta >= 0 else "#ef4444"
    bg_color = "#fef2f2" if regression_detected else "#f0fdf4"
    header_color = "#dc2626" if regression_detected else "#16a34a"

    trend_html = ""
    if yesterday_delta is not None:
        change = avg_delta - yesterday_delta
        trend_emoji = "📈" if change > 0 else ("📉" if change < 0 else "➡️")
        trend_html = f"""
        <tr>
            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">vs Yesterday</td>
            <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">
                {trend_emoji} {change:+.3f} (yesterday was {yesterday_delta:+.3f})
            </td>
        </tr>"""

    regression_banner = ""
    if regression_detected:
        regression_banner = """
        <div style="background:#dc2626;color:white;padding:16px;border-radius:8px;margin-bottom:16px;font-size:16px;">
            ⚠️ REGRESSION DETECTED — Our agent's accuracy dropped vs yesterday.
            Investigate immediately.
        </div>"""

    latency_banner = ""
    if latency_breaches:
        latency_banner = f"""
        <div style="background:#b45309;color:white;padding:16px;border-radius:8px;margin-bottom:16px;font-size:15px;">
            ⚠️ Latency budget breach — {latency_breaches} case(s) over their time budget:
            {", ".join(latency_cases)}
        </div>"""

    return f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        {regression_banner}
        {latency_banner}
        <div style="background: {bg_color}; border-radius: 12px; padding: 24px; margin-bottom: 16px;">
            <h2 style="color: {header_color}; margin: 0 0 16px 0;">
                Agent vs MCP Benchmark — {run_date}
            </h2>
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; font-weight: 600;">Avg Δ Accuracy</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; color: {delta_color}; font-size: 24px; font-weight: 700;">
                        {avg_delta:+.3f}
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">Ours Wins</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{ours_wins}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">MCP Wins</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb; {"color: #dc2626; font-weight: 600;" if mcp_wins > 0 else ""}">{mcp_wins}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">Ties</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{ties}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">Failures</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{failures}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">Total Cases</td>
                    <td style="padding: 8px; border-bottom: 1px solid #e5e7eb;">{total}</td>
                </tr>
                {trend_html}
            </table>
        </div>

        <p style="color: #6b7280; font-size: 13px;">
            Run ID: {stats.get("run_id", "n/a")}<br>
            View full results: <a href="https://staging.suitestudio.ai/settings/benchmarks">Dashboard</a><br>
            API: <code>GET /api/v1/benchmarks/latest</code>
        </p>
    </body>
    </html>
    """


def _send_email(config: dict, subject: str, html_body: str) -> bool:
    """Send an HTML email via SMTP. Returns True on success."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["from"]
    msg["To"] = ", ".join(config["to"])
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=30) as server:
            server.ehlo()
            if config["port"] == 587:
                server.starttls()
                server.ehlo()
            if config["user"] and config["password"]:
                server.login(config["user"], config["password"])
            server.sendmail(config["from"], config["to"], msg.as_string())

        logger.info(
            "benchmark_email.sent",
            subject=subject,
            to=config["to"],
        )
        return True
    except Exception as exc:
        logger.error(
            "benchmark_email.send_failed",
            error=str(exc),
            subject=subject,
        )
        return False
