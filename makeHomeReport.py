"""
Generate the main homepage (index.html) for the ConsolidatedHTML directory.
Shows a stacked bar chart of consumption by service name over the last 30 days,
with a top menu linking to monthly reports and per-user dashboards.
Reads CSV data from ConsolidatedCSV.
"""

import json
from pathlib import Path
from collections import defaultdict

from consolidateReports import get_usd_conversion_rate, to_display_cost
from perUserReport import (
    find_most_recent_daily_report,
    find_daily_reports_last_30_days,
    load_consolidated_csv,
)
from reportNav import find_latest_monthly_report, get_top_nav_html, get_top_nav_css


def build_daily_cost_by_service_overall(
    consolidated_dir: Path,
    most_recent_date_str: str,
) -> list[dict]:
    """
    Build daily cost data for the last 30 days, grouped by ServiceName (all users).
    Returns list of { "date": "YYYY-MM-DD", "services": { "SERVICENAME": cost } }
    sorted by date ascending.
    """
    daily_reports = find_daily_reports_last_30_days(consolidated_dir, most_recent_date_str)
    result = []
    for csv_path, date_str in daily_reports:
        try:
            rows = load_consolidated_csv(csv_path)
        except OSError:
            continue
        by_service: dict[str, float] = defaultdict(float)
        for row in rows:
            svc = (row.get("ServiceName") or "").strip() or "(Uncategorized)"
            try:
                cost = to_display_cost(float(row.get("BilledCost") or 0))
            except (ValueError, TypeError):
                cost = 0
            by_service[svc] += cost
        result.append({"date": date_str, "services": dict(by_service)})
    return result


def _daily_bar_chart_html(daily_cost_data: list[dict], currency: str) -> str:
    """Generate full-width stacked bar chart HTML for cost per day by ServiceName."""
    if not daily_cost_data:
        return ""

    # Collect all service names across all days, sort by total cost descending
    service_totals: dict[str, float] = defaultdict(float)
    for day in daily_cost_data:
        for svc, cost in (day.get("services") or {}).items():
            service_totals[svc] += cost
    service_order = sorted(service_totals.keys(), key=lambda s: -service_totals[s])

    colors = [
        "#1a365d", "#2c5282", "#2b6cb0", "#3182ce", "#4299e1",
        "#63b3ed", "#90cdf4", "#bee3f8", "#ebf8ff", "#065f46",
        "#047857", "#059669", "#10b981", "#34d399", "#581c87",
        "#6b21a8", "#7e22ce", "#9333ea", "#a855f7", "#c084fc",
    ]
    labels = [d["date"] for d in daily_cost_data]
    datasets_js = []
    for i, svc in enumerate(service_order):
        values = []
        for d in daily_cost_data:
            values.append(round((d.get("services") or {}).get(svc, 0), 2))
        color = colors[i % len(colors)]
        datasets_js.append(
            f'{{ label: {json.dumps(svc)}, data: {json.dumps(values)}, '
            f'backgroundColor: "{color}", borderWidth: 0 }}'
        )

    return f'''
    <div class="daily-bar-chart-section">
        <h2 style="margin: 2rem 0 1rem; font-size: 1rem;">Cost per day (last 30 days, stacked by Service Name)</h2>
        <div class="daily-bar-chart-wrapper">
            <canvas id="dailyBarChart" height="280"></canvas>
        </div>
    </div>
    <script>
        (function() {{
            const dailyBarCtx = document.getElementById('dailyBarChart');
            if (dailyBarCtx) {{
                new Chart(dailyBarCtx.getContext('2d'), {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(labels)},
                        datasets: [{', '.join(datasets_js)}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{ display: false }},
                            tooltip: {{ mode: 'index', callbacks: {{ label: (ctx) => ctx.raw > 0 ? ctx.dataset.label + ': ' + ctx.raw.toFixed(2) + ' {currency}' : '' }} }}
                        }},
                        scales: {{
                            x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ maxRotation: 45, minRotation: 45 }} }},
                            y: {{ stacked: true, beginAtZero: true, ticks: {{ callback: (v) => v + ' {currency}' }} }}
                        }}
                    }}
                }});
            }}
        }})();
    </script>'''


def generate_homepage(
    consolidated_dir: Path,
    date_str: str,
    daily_cost_data: list[dict],
    total_30day: float,
    output_path: Path,
) -> None:
    """Generate the main index.html homepage."""
    currency = "USD" if get_usd_conversion_rate() is not None else "USD"
    monthly_report = find_latest_monthly_report(consolidated_dir) or "2026/2026-03.html"
    top_nav = get_top_nav_html("", monthly_report)

    bar_chart_html = _daily_bar_chart_html(daily_cost_data, currency)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FOCUS Reports - Home</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
        body {{ margin: 0; padding: 0; background: #f8fafc; color: #1e293b; }}
        {get_top_nav_css()}
        .main {{ padding: 2rem; max-width: 1400px; margin: 0 auto; }}
        h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; color: #0f172a; }}
        .subtitle {{ color: #64748b; margin-bottom: 2rem; }}
        .total-cost {{ font-size: 1.25rem; font-weight: 600; color: #0c4a6e; margin-bottom: 1rem; }}
        .daily-bar-chart-section {{ width: 100%; margin: 2rem 0; }}
        .daily-bar-chart-wrapper {{ width: 100%; height: 280px; background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        @media (max-width: 768px) {{ .top-nav {{ flex-wrap: wrap; }} .main {{ padding: 1rem; }} }}
    </style>
</head>
<body>
{top_nav}
    <main class="main">
        <h1>FOCUS Reports</h1>
        <p class="subtitle">Consumption overview · Based on data through {date_str}</p>
        <div class="total-cost">Total cost (last 30 days): {total_30day:.2f} {currency}</div>
{bar_chart_html}
    </main>
</body>
</html>
"""

    output_path.write_text(html_content, encoding="utf-8")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    consolidated_csv_dir = script_dir / "FocusReports" / "ConsolidatedCSV"
    consolidated_html_dir = script_dir / "FocusReports" / "ConsolidatedHTML"

    result = find_most_recent_daily_report(consolidated_csv_dir)
    if not result:
        print("No daily consolidated report found under FocusReports/ConsolidatedCSV")
        return

    csv_path, date_str = result
    print(f"Using most recent daily report: {csv_path.name} ({date_str})")

    daily_cost_data = build_daily_cost_by_service_overall(consolidated_csv_dir, date_str)
    total_30day = sum(
        sum((d.get("services") or {}).values())
        for d in daily_cost_data
    )

    output_path = consolidated_html_dir / "index.html"
    generate_homepage(
        consolidated_html_dir, date_str, daily_cost_data, total_30day, output_path
    )

    print(f"Generated: {output_path}")
    print("-" * 60)
    print(f"Done. Open {output_path} to view.")


if __name__ == "__main__":
    main()
