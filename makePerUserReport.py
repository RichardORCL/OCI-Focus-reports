"""
Generate per-user dashboards in ConsolidatedHTML/users based on the most recent daily
consolidated report (from ConsolidatedCSV). Each user gets two dashboards (last day and last 30 days)
with a toggle to switch between them.
"""

from __future__ import annotations
import csv
import html
import json
import re
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

# Import shared logic from consolidateReports
from consolidateReports import (
    TABLE_COLUMNS,
    COLUMN_LABELS,
    attach_master_console_links,
    build_rows_filter_item,
    cost_by_service_category,
    cost_by_compartment,
    cost_by_region,
    cost_by_age,
    enrich_rows_with_master_resource,
    enrich_rows_with_resource_names,
    format_created_date_display,
    combined_items_info_btn_html,
    deleted_resource_icon_html,
    get_combined_items_js_helpers,
    get_combined_items_modal_css,
    get_combined_items_modal_html,
    get_deleted_resource_icon_css,
    get_usd_conversion_rate,
    masters_with_multiple_resources,
    load_config,
    load_resource_master_lookups,
    load_resource_names_lookup,
    load_resource_type_console_prefixes,
    master_oci_console_link_html,
    oci_console_link_html,
    to_display_cost,
)
# Resource ↗ console links: oci_console_link_html() in consolidateReports (uses build_oci_console_href).
# Same rules as consolidated HTML — including query-style url_prefix (with "?"): OCID is appended with no
# slash before it (e.g. loganalytics/...&comp=ocid...). No separate URL logic in this module.
from reportNav import (
    find_latest_monthly_report,
    get_filter_help_modal_css,
    get_filter_help_modal_html,
    get_filter_help_modal_init_js,
    get_filter_help_nav_icon_html,
    get_top_nav_css,
    get_top_nav_html,
)
from makeStorageReport import load_unattached_volume_ocid_set, volume_delete_cli_command

UNATTACHED_VOLUME_RESOURCE_TYPES = frozenset({"volume", "bootvolume"})
UNATTACHED_VOLUME_TOOLTIP = "This Volume is NOT attached to any compute instance"


def is_unattached_volume_row(row: dict, unattached_ocids: frozenset[str]) -> bool:
    """True when row is a block/boot volume whose ResourceId is in the storage report JSON."""
    rtype = (row.get("ResourceType") or "").strip().lower()
    if rtype not in UNATTACHED_VOLUME_RESOURCE_TYPES:
        return False
    rid = (row.get("ResourceId") or "").strip()
    return bool(rid and rid in unattached_ocids)


def unattached_volume_delete_cli(row: dict, unattached_ocids: frozenset[str]) -> str:
    """OCI CLI delete command for an unattached block/boot volume row, or empty string."""
    if not is_unattached_volume_row(row, unattached_ocids):
        return ""
    rid = (row.get("ResourceId") or "").strip()
    region = (row.get("Region") or "").strip()
    rtype = (row.get("ResourceType") or "").strip().lower()
    kind = "boot" if rtype == "bootvolume" else "block"
    return volume_delete_cli_command(rid, region, kind)


def unattached_volume_indicator_html() -> str:
    """Leading ! marker for unattached block/boot volumes in resource name cells."""
    return (
        f'<span class="unattached-volume-indicator" '
        f'title="{html.escape(UNATTACHED_VOLUME_TOOLTIP)}" '
        f'aria-label="Unattached volume">!</span>'
    )


def get_day_margin() -> int:
    """Return DayMargin from config.json (days before most recent to use). Default 1."""
    config = load_config()
    val = config.get("DayMargin")
    if val is None:
        return 1
    try:
        n = int(val)
        return max(0, n)
    except (TypeError, ValueError):
        return 1


def find_most_recent_daily_report(consolidated_dir: Path) -> tuple[Path, str] | None:
    """
    Find the most recent daily consolidated CSV (YYYY/MM/YYYY-MM-DD.csv).
    Returns (path, date_str) or None.
    """
    pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.csv$")
    candidates: list[tuple[Path, tuple[int, int, int]]] = []

    for csv_path in consolidated_dir.rglob("*.csv"):
        rel = csv_path.relative_to(consolidated_dir)
        parts = rel.parts
        if len(parts) >= 3:
            try:
                year, month = int(parts[0]), int(parts[1])
                if csv_path.name == f"{year:04d}-{month:02d}.csv":
                    continue  # Skip monthly reports
            except (ValueError, IndexError):
                pass
        m = pattern.match(csv_path.name)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            candidates.append((csv_path, (y, mo, d)))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    path, (y, mo, d) = candidates[0]
    return path, f"{y:04d}-{mo:02d}-{d:02d}"


def find_daily_report_for_date(consolidated_dir: Path, date_str: str) -> Path | None:
    """
    Find the daily consolidated CSV for a specific date (YYYY-MM-DD).
    Returns path or None if not found.
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if not m:
        return None
    year, month, day = m.group(1), m.group(2), m.group(3)
    # Check common locations: YYYY/MM/YYYY-MM-DD.csv
    candidate = consolidated_dir / year / month / f"{year}-{month}-{day}.csv"
    if candidate.exists():
        return candidate
    for csv_path in consolidated_dir.rglob(f"{year}-{month}-{day}.csv"):
        rel = csv_path.relative_to(consolidated_dir)
        parts = rel.parts
        if len(parts) >= 3 and csv_path.name == f"{year}-{month}-{day}.csv":
            return csv_path
    return None


def find_daily_reports_last_30_days(
    consolidated_dir: Path,
    most_recent_date_str: str,
) -> list[tuple[Path, str]]:
    """
    Find daily consolidated CSVs for the last 30 days (if they exist).
    Returns list of (path, date_str) sorted chronologically (oldest first).
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", most_recent_date_str)
    if not m:
        return []
    end_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    start_date = end_date - timedelta(days=29)  # 30 days inclusive

    path_by_date: dict[tuple[int, int, int], Path] = {}
    pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.csv$")
    for csv_path in consolidated_dir.rglob("*.csv"):
        rel = csv_path.relative_to(consolidated_dir)
        parts = rel.parts
        if len(parts) >= 3:
            try:
                year, month = int(parts[0]), int(parts[1])
                if csv_path.name == f"{year:04d}-{month:02d}.csv":
                    continue  # Skip monthly reports
            except (ValueError, IndexError):
                pass
        match = pattern.match(csv_path.name)
        if match:
            y, mo, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
            dte = date(y, mo, d)
            if start_date <= dte <= end_date:
                path_by_date[(y, mo, d)] = csv_path

    result = []
    for dte in (start_date + timedelta(days=i) for i in range(30)):
        key = (dte.year, dte.month, dte.day)
        if key in path_by_date:
            result.append((path_by_date[key], dte.strftime("%Y-%m-%d")))
    return result


def _normalize_owner_key(owner: str) -> str:
    return (owner or "").strip() or "(No owner)"


def build_30day_rollup_for_owners(
    consolidated_dir: Path,
    end_date_str: str,
    owners: set[str] | frozenset,
) -> tuple[float, dict[str, list[dict]]]:
    """
    Single pass over the last-30-days daily CSVs: total spend (all users) and, per owner,
    the same shape as build_daily_cost_by_service — list of
    { "date": "YYYY-MM-DD", "services": { ServiceName: cost } } in chronological order.

    Avoids O(owners × rglob × reads): one rglob, each daily file is read once.
    """
    daily_reports = find_daily_reports_last_30_days(consolidated_dir, end_date_str)
    owner_keys = {_normalize_owner_key(o) for o in owners}
    per_owner: dict[str, list[dict]] = {o: [] for o in owner_keys}
    total_30day = 0.0

    for csv_path, date_str in daily_reports:
        try:
            rows = load_consolidated_csv(csv_path)
        except OSError:
            for o in owner_keys:
                per_owner[o].append({"date": date_str, "services": {}})
            continue

        day_by_owner: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rows:
            try:
                cost = to_display_cost(float(row.get("BilledCost") or 0))
            except (ValueError, TypeError):
                cost = 0.0
            total_30day += cost
            row_owner = (row.get("OwnerCreatedBy") or "").strip() or "(No owner)"
            svc = (row.get("ServiceName") or "").strip() or "(Uncategorized)"
            day_by_owner[row_owner][svc] += cost

        for o in owner_keys:
            by_svc = day_by_owner.get(o)
            per_owner[o].append(
                {"date": date_str, "services": dict(by_svc) if by_svc else {}}
            )

    return total_30day, per_owner


def load_consolidated_csv(path: Path) -> list[dict]:
    """Load consolidated CSV and return list of row dicts."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            reader.fieldnames = [(fn or "").lstrip("\ufeff").strip() for fn in reader.fieldnames]
        for row in reader:
            rows.append(row)
    return rows


def sanitize_owner_filename(owner: str) -> str:
    """Convert owner string to a safe filename (URL-safe, no path traversal)."""
    if not owner or not str(owner).strip():
        return "no-owner"
    s = str(owner).strip()
    s = re.sub(r"[^\w\-_.]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:120] if s else "no-owner"


def group_rows_by_owner(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by OwnerCreatedBy. Empty owner -> '(No owner)'."""
    by_owner: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        owner = (row.get("OwnerCreatedBy") or "").strip()
        if not owner:
            owner = "(No owner)"
        by_owner[owner].append(row)
    return dict(by_owner)


def build_owner_30day_resource_rows(
    consolidated_dir: Path,
    end_date_str: str,
    owner: str,
) -> list[dict]:
    """
    One row per (ResourceId, SkuId) for owner across the last 30 days.
    BilledCost is summed across all days for that charge line; other columns come from the most recent day.
    Keeping SkuId separate preserves multi-SKU lines so Master (i) / Combined groups work like the day view.
    """
    daily_reports = find_daily_reports_last_30_days(consolidated_dir, end_date_str)
    owner_key = _normalize_owner_key(owner)
    by_key: dict[tuple[str, str], dict] = {}

    for _csv_path, report_date in daily_reports:
        try:
            day_rows = load_consolidated_csv(_csv_path)
        except OSError:
            continue
        for row in day_rows:
            if _normalize_owner_key(row.get("OwnerCreatedBy") or "") != owner_key:
                continue
            rid = (row.get("ResourceId") or "").strip()
            if not rid:
                continue
            sku = (row.get("SkuId") or "").strip()
            try:
                cost = to_display_cost(float(row.get("BilledCost") or 0))
            except (ValueError, TypeError):
                cost = 0.0
            key = (rid, sku)
            entry = by_key.get(key)
            if entry is None:
                by_key[key] = {"cost": cost, "latest_date": report_date, "row": dict(row)}
            else:
                entry["cost"] += cost
                if report_date >= entry["latest_date"]:
                    entry["latest_date"] = report_date
                    entry["row"] = dict(row)

    result: list[dict] = []
    for entry in by_key.values():
        row = dict(entry["row"])
        row["BilledCost"] = entry["cost"]
        result.append(row)
    return result


def _daily_bar_chart_html(
    daily_cost_data: list[dict],
    currency: str,
    report_date_str: str | None = None,
) -> str:
    """Generate full-width stacked bar chart HTML for cost per day by ServiceName.
    If report_date_str is set, the bar for that date is highlighted in green."""
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
    highlight_color = "#22c55e"  # Green for report date
    datasets_js = []
    for i, svc in enumerate(service_order):
        values = []
        for d in daily_cost_data:
            values.append(round((d.get("services") or {}).get(svc, 0), 2))
        color = colors[i % len(colors)]
        # Use array of colors to highlight report date bar in green
        if report_date_str:
            bg_colors_js = json.dumps([
                highlight_color if d["date"] == report_date_str else color
                for d in daily_cost_data
            ])
            datasets_js.append(
                f'{{ label: {json.dumps(svc)}, data: {json.dumps(values)}, '
                f'backgroundColor: {bg_colors_js}, borderWidth: 0 }}'
            )
        else:
            datasets_js.append(
                f'{{ label: {json.dumps(svc)}, data: {json.dumps(values)}, '
                f'backgroundColor: "{color}", borderWidth: 0 }}'
            )

    report_note = " (report date in green)" if report_date_str else ""
    return f'''
    <div class="daily-bar-chart-section">
        <h2 style="margin: 2rem 0 1rem; font-size: 1rem;">Cost per day (last 30 days, stacked by Service Name){report_note}</h2>
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


def generate_per_user_html(
    rows: list[dict],
    owner: str,
    date_str: str,
    output_path: Path,
    total_day_cost: float = 0,
    daily_cost_data: list[dict] | None = None,
    total_30day_overall: float = 0,
    consolidated_dir: Path | None = None,
    rank_daily: int = 0,
    rank_30day: int = 0,
    total_owners: int = 0,
    unattached_volume_ocids: frozenset[str] | None = None,
    master_resource_lookup: dict[str, str] | None = None,
    master_resource_id_lookup: dict[str, str] | None = None,
    master_resource_type_lookup: dict[str, str] | None = None,
    master_resource_charge_lookup: dict[str, str] | None = None,
    resource_names_lookup: dict[str, str] | None = None,
    cost_mode: str = "day",
    daily_owner_cost: float = 0,
) -> None:
    """Generate HTML dashboard for a single user's resources.

    cost_mode is "day" (report-date snapshot) or "30day" (all resources in last 30 days,
    one row per ResourceId+SkuId with summed BilledCost).

    OCI console links on resource names use the same resourceTypeOciConsole.csv rules as consolidated reports
    (ServiceName + ResourceType match, or ServiceCategory if ServiceName is empty).

    Block/boot volume rows (ResourceType volume or bootvolume) whose ResourceId appears in
    consolidateReports/StorageReport/unattached_volumes.json get a leading ! on the resource cell;
    hover shows that the volume is not attached to any compute instance.
    """
    is_30day = cost_mode == "30day"
    safe_basename = output_path.stem
    alternate_href = f"{safe_basename.removesuffix('_30d')}.html" if is_30day else f"{safe_basename}_30d.html"
    unattached: frozenset[str] = unattached_volume_ocids or frozenset()
    enrich_rows_with_resource_names(rows, lookup=resource_names_lookup)
    enrich_rows_with_master_resource(
        rows,
        lookup=master_resource_lookup,
        names_lookup=resource_names_lookup,
        id_lookup=master_resource_id_lookup,
        type_lookup=master_resource_type_lookup,
        charge_lookup=master_resource_charge_lookup,
    )
    oci_console_prefixes = load_resource_type_console_prefixes()
    attach_master_console_links(rows, oci_console_prefixes)
    _filter_help_icon = get_filter_help_nav_icon_html()
    if consolidated_dir is None:
        consolidated_dir = output_path.parent.parent
    nav_base_path = "../"
    monthly_report = find_latest_monthly_report(consolidated_dir) or "2026/2026-03.html"
    top_nav = get_top_nav_html(nav_base_path, monthly_report)

    # Table columns - exclude OwnerCreatedBy for per-user view (redundant)
    display_cols = [c for c in TABLE_COLUMNS if c not in ("OwnerCreatedBy", "PctOfDayUsed")]

    cost_data = cost_by_service_category(rows)
    comp_data = cost_by_compartment(rows)
    region_data = cost_by_region(rows)
    age_data = cost_by_age(rows)
    total_cost = sum(c for _, c in cost_data)
    currency = (
        "USD"
        if get_usd_conversion_rate() is not None
        else ((rows[0].get("BillingCurrency") or "USD") if rows else "USD")
    )

    labels = [html.escape(cat) for cat, _ in cost_data]
    values = [round(c, 2) for _, c in cost_data]
    comp_labels = [html.escape(c) for c, _ in comp_data]
    comp_values = [round(c, 2) for _, c in comp_data]
    region_labels = [html.escape(r) for r, _ in region_data]
    region_values = [round(c, 2) for _, c in region_data]
    age_labels = [html.escape(a) for a, _ in age_data]
    age_values = [round(c, 2) for _, c in age_data]

    colors = [
        "#1a365d", "#2c5282", "#2b6cb0", "#3182ce", "#4299e1",
        "#63b3ed", "#90cdf4", "#bee3f8", "#ebf8ff", "#e0e7ff",
    ]
    comp_colors = [
        "#065f46", "#047857", "#059669", "#10b981", "#34d399",
        "#6ee7b7", "#a7f3d0", "#d1fae5", "#ecfdf5", "#064e3b",
    ]
    region_colors = [
        "#581c87", "#6b21a8", "#7e22ce", "#9333ea", "#a855f7",
        "#c084fc", "#d8b4fe", "#e9d5ff", "#f3e8ff", "#4c1d95",
    ]
    # Green for <6 months (younger=light, older=darker), red for older, gray for unknown (label-based so colors persist when filtering)
    age_colors = [
        "#4ade80", "#22c55e", "#166534",  # <7 days (light), <31 days, <6 months (dark)
        "#b91c1c", "#c62828", "#dc2626",  # <1 year, 1 to 2 years, 2+ years
        "#94a3b8",                         # (Unknown age)
    ]
    age_bucket_to_color = {
        "<7 days": "#4ade80", "<31 days": "#22c55e", "<6 months": "#166534",
        "<1 year": "#b91c1c", "1 to 2 years": "#c62828", "2+ years": "#dc2626", "(Unknown age)": "#94a3b8",
    }

    rows_filter_data = []
    for r in rows:
        item = build_rows_filter_item(r, display_cols)
        item["unattachedVolume"] = is_unattached_volume_row(r, unattached)
        delete_cli = unattached_volume_delete_cli(r, unattached)
        if delete_cli:
            item["deleteCli"] = delete_cli
        rows_filter_data.append(item)
    multi_resource_masters = masters_with_multiple_resources(rows)

    # Dropdown options for chart filters
    def _dropdown_opts(items: list[tuple[str, float]]) -> str:
        opts = '<option value="">(All)</option>'
        for label, _ in items:
            opts += f'<option value="{html.escape(label)}">{html.escape(label)}</option>'
        return opts

    cat_dropdown = _dropdown_opts(cost_data)
    comp_dropdown = _dropdown_opts(comp_data)
    region_dropdown = _dropdown_opts(region_data)
    age_dropdown = _dropdown_opts(age_data)
    raw_labels = [c for c, _ in cost_data]
    raw_comp_labels = [c for c, _ in comp_data]
    raw_region_labels = [r for r, _ in region_data]
    raw_age_labels = [a for a, _ in age_data]

    pct_of_day = (total_cost / total_day_cost * 100) if total_day_cost > 0 else 0
    total_30day_user = sum(
        sum((d.get("services") or {}).values())
        for d in (daily_cost_data or [])
    )
    pct_30day = (total_30day_user / total_30day_overall * 100) if total_30day_overall > 0 else 0

    rank_daily_str = f" · Rank #{rank_daily} of {total_owners}" if (rank_daily and total_owners) else ""
    rank_30day_str = f" · Rank #{rank_30day} of {total_owners}" if (rank_30day and total_owners) else ""

    overall_cost_for_pct = total_30day_overall if is_30day else total_day_cost
    primary_total = total_30day_user if is_30day else total_cost
    primary_pct = pct_30day if is_30day else pct_of_day
    primary_rank_str = rank_30day_str if is_30day else rank_daily_str
    primary_label = "Total cost (last 30 days)" if is_30day else "Current Daily cost"
    secondary_label = "Current Daily cost" if is_30day else "Total cost (last 30 days)"
    secondary_total = daily_owner_cost if is_30day else total_30day_user
    secondary_pct = (
        (daily_owner_cost / total_day_cost * 100) if is_30day and total_day_cost > 0 else pct_30day
    )
    secondary_rank_str = rank_daily_str if is_30day else rank_30day_str
    page_title = f"Resources for {owner} (last 30 days)" if is_30day else f"Current resources for {owner}"
    subtitle = (
        f"All resources billed to this user in the last 30 days ending {date_str} · {len(rows)} resources"
        if is_30day
        else f"Based on consolidated report dated {date_str} (most recent) · {len(rows)} resources"
    )

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(page_title)}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
        body {{ margin: 0; padding: 0; background: #f8fafc; color: #1e293b; }}
        h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; color: #0f172a; }}
        .subtitle {{ color: #64748b; margin-bottom: 2rem; }}
        .back-link {{ display: inline-block; margin-bottom: 1rem; color: #0f172a; text-decoration: none; font-weight: 500; }}
        .back-link:hover {{ text-decoration: underline; }}
        .charts-row {{ display: flex; flex-wrap: wrap; gap: 2rem; margin-bottom: 2rem; }}
        .chart-container {{ flex: 1; min-width: 240px; max-width: 320px; }}
        .chart-wrapper {{ background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        .chart-filter {{ margin-top: 0.75rem; }}
        .chart-filter select {{ width: 100%; padding: 0.35rem 0.5rem; font-size: 0.8rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .chart-title-row {{ display: flex; align-items: center; justify-content: space-between; margin: 0 0 1rem; font-size: 1rem; }}
        .reset-filter {{ cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0.2rem; line-height: 1; user-select: none; border-radius: 4px; margin-left: 0.25rem; }}
        .reset-filter:hover {{ opacity: 1; background: rgba(0,0,0,0.06); }}
        .reset-all-filters {{ cursor: pointer; display: inline-block; padding: 0.2rem; opacity: 0.6; font-size: 1.1rem; }}
        .reset-all-filters:hover {{ opacity: 1; }}
        .total-cost {{ font-size: 1.25rem; font-weight: 600; color: #0c4a6e; margin-bottom: 0.25rem; }}
        .total-cost-30d {{ font-size: 1rem; font-weight: 500; color: #475569; margin-bottom: 0.25rem; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        th {{ background: #0f172a; color: white; padding: 0.75rem 1rem; text-align: left; font-weight: 500; }}
        td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }}
        tr:hover {{ background: #f8fafc; }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .resource-name {{ cursor: help; }}
        .copy-resource-btn {{ cursor: pointer; margin-left: 0.25rem; opacity: 0.5; font-size: 0.75em; padding: 0 0.2rem; border: none; background: none; vertical-align: middle; }}
        .copy-resource-btn:hover {{ opacity: 1; }}
        .oci-console-link {{ color: #2563eb; text-decoration: none; margin-right: 0.35rem; font-size: 0.85em; opacity: 0.65; vertical-align: middle; }}
        .oci-console-link:hover {{ opacity: 1; text-decoration: underline; }}
        {get_combined_items_modal_css()}
        {get_deleted_resource_icon_css()}
        .unattached-volume-indicator {{
            display: inline-block;
            color: #c2410c;
            font-weight: 800;
            font-size: 1.1em;
            line-height: 1;
            margin-right: 0.3rem;
            vertical-align: middle;
            cursor: help;
        }}
        .compartment-cell {{ cursor: help; }}
        .resource-name-col {{ width: 22ch; max-width: 22ch; overflow: hidden; text-overflow: ellipsis; }}
        .created-date-col {{ width: 16ch; max-width: 16ch; white-space: nowrap; }}
        .truncate-20-col {{ width: 20ch; max-width: 20ch; overflow: hidden; text-overflow: ellipsis; }}
        .truncate-cell {{ cursor: help; }}
        th.sortable {{ cursor: pointer; user-select: none; }}
        th.sortable:hover {{ background: #1e293b; }}
        th.sorted-asc::after {{ content: ' \\25B2'; font-size: 0.7em; opacity: 0.8; }}
        th.sorted-desc::after {{ content: ' \\25BC'; font-size: 0.7em; opacity: 0.8; }}
        .filter-row select {{ width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font-size: 0.875rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .table-tools-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 1.5rem; margin-bottom: 0.75rem; }}
        .table-view-toggle {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
        .hide-zero-cost-label {{ display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; color: #475569; cursor: pointer; user-select: none; }}
        .hide-zero-cost-label input {{ width: 1rem; height: 1rem; accent-color: #0f172a; cursor: pointer; }}
        .table-toggle-btn {{ padding: 0.4rem 0.75rem; font-size: 0.875rem; border: 1px solid #e2e8f0; border-radius: 6px; background: white; color: #64748b; cursor: pointer; }}
        .table-toggle-btn:hover {{ background: #f8fafc; color: #0f172a; border-color: #cbd5e1; }}
        .table-toggle-btn.active {{ background: #0f172a; color: white; border-color: #0f172a; }}
        .delete-volume-cli-btn {{ cursor: pointer; margin-left: 0.25rem; opacity: 0.5; font-size: 0.85em; padding: 0 0.2rem; border: none; background: none; vertical-align: middle; }}
        .delete-volume-cli-btn:hover {{ opacity: 1; }}
        .bulk-delete-cli-btn {{ cursor: pointer; font-size: 0.875rem; padding: 0.45rem 0.85rem; border: 1px solid #cbd5e1; border-radius: 6px; background: white; color: #0f172a; }}
        .bulk-delete-cli-btn:hover {{ background: #f8fafc; border-color: #94a3b8; }}
        .bulk-delete-cli-btn:disabled {{ cursor: not-allowed; opacity: 0.55; }}
        .cost-period-toggle {{ display: inline-flex; align-items: center; gap: 0.65rem; padding: 0.45rem 0.75rem; background: white; border: 1px solid #e2e8f0; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
        .cost-period-label {{ font-size: 0.875rem; color: #64748b; user-select: none; }}
        .cost-period-label.active {{ color: #0f172a; font-weight: 600; }}
        .cost-period-switch {{ position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }}
        .cost-period-switch input {{ opacity: 0; width: 0; height: 0; }}
        .cost-period-slider {{ position: absolute; cursor: pointer; inset: 0; background: #cbd5e1; border-radius: 999px; transition: background 0.2s; }}
        .cost-period-slider::before {{ position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: transform 0.2s; box-shadow: 0 1px 2px rgba(0,0,0,0.15); }}
        .cost-period-switch input:checked + .cost-period-slider {{ background: #0f172a; }}
        .cost-period-switch input:checked + .cost-period-slider::before {{ transform: translateX(20px); }}
        .grouped-table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        .grouped-table th, .grouped-table td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }}
        .grouped-table th {{ background: #0f172a; color: white; }}
        .daily-bar-chart-section {{ width: 100%; margin: 2rem 0; }}
        .daily-bar-chart-wrapper {{ width: 100%; height: 280px; background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        #resourcesTable {{ font-size: 0.8rem; }}
        @media (max-width: 768px) {{ table {{ font-size: 0.875rem; }} }}
        {get_top_nav_css()}
        {get_filter_help_modal_css()}
        .report-content {{ padding: 2rem; }}
        .useronly .top-nav, .useronly .back-link {{ display: none !important; }}
    </style>
    <script>
        (function() {{
            if (new URLSearchParams(window.location.search).has('useronly')) {{
                document.documentElement.classList.add('useronly');
            }}
        }})();
    </script>
</head>
<body>
{top_nav}
    <div class="report-content">
    <a href="index.html" class="back-link">&#9664; All users</a>
    <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap;">
        <div>
            <h1>{html.escape(page_title)}</h1>
            <p class="subtitle">{subtitle}</p>
        </div>
        <div style="display: flex; align-items: center; gap: 0.75rem; flex-shrink: 0; flex-wrap: wrap;">
        <div class="cost-period-toggle" title="Switch between last day and last 30 days cost view">
            <span class="cost-period-label{"" if is_30day else " active"}" id="costPeriodLabelDay">Last day</span>
            <label class="cost-period-switch" aria-label="Switch cost period">
                <input type="checkbox" id="costPeriodSwitch" {"checked" if is_30day else ""} />
                <span class="cost-period-slider"></span>
            </label>
            <span class="cost-period-label{" active" if is_30day else ""}" id="costPeriodLabel30d">Last 30 days</span>
        </div>
        <span class="reset-all-filters" title="Reset all filters">&#8635;</span>
        {_filter_help_icon}
        </div>
    </div>
    <div class="total-cost" id="totalCost">{primary_label}: {primary_total:.2f} {currency} ({primary_pct:.1f}% of overall spend){primary_rank_str}</div>
    <div class="total-cost total-cost-30d" id="totalCost30d">{secondary_label}: {secondary_total:.2f} {currency} ({secondary_pct:.1f}% of overall spend){secondary_rank_str}</div>
    <div class="charts-row">
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Service</h2><span class="reset-filter" data-reset="cat" title="Reset Service filter">↺</span></div>
                <canvas id="costChart" width="400" height="280"></canvas>
                <div class="chart-filter"><select id="filterServiceCategory" aria-label="Filter by Service">""" + cat_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Compartment</h2><span class="reset-filter" data-reset="comp" title="Reset Compartment filter">↺</span></div>
                <canvas id="compartmentChart" width="400" height="280"></canvas>
                <div class="chart-filter"><select id="filterCompartment" aria-label="Filter by Compartment">""" + comp_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Region</h2><span class="reset-filter" data-reset="region" title="Reset Region filter">↺</span></div>
                <canvas id="regionChart" width="400" height="280"></canvas>
                <div class="chart-filter"><select id="filterRegion" aria-label="Filter by Region">""" + region_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Age</h2><span class="reset-filter" data-reset="age" title="Reset Age filter">↺</span></div>
                <canvas id="ageChart" width="400" height="280"></canvas>
                <div class="chart-filter"><select id="filterAge" aria-label="Filter by Age">""" + age_dropdown + """</select></div>
            </div>
        </div>
    </div>
""" + (_daily_bar_chart_html(daily_cost_data or [], currency, date_str) if daily_cost_data else "") + """
    <h2 style="margin: 2rem 0 1rem; font-size: 1rem;" id="resourcesTableHeading">All Resources</h2>
    <div class="table-tools-row">
    <div class="table-view-toggle">
        <button type="button" class="table-toggle-btn" data-mode="all" aria-pressed="false">All resources</button>
        <button type="button" class="table-toggle-btn" data-mode="groupedService" aria-pressed="false">Grouped by Service Name</button>
        <button type="button" class="table-toggle-btn" data-mode="groupedResourceType" aria-pressed="false">Grouped by Resource Type</button>
        <button type="button" class="table-toggle-btn active" data-mode="groupedMaster" aria-pressed="true">Grouped by Master Resource</button>
        <button type="button" class="table-toggle-btn" data-mode="unattachedVolumes" aria-pressed="false">Volumes not Attached</button>
        <button type="button" id="copyFilteredDeleteCommandsBtn" class="bulk-delete-cli-btn" style="display: none;">Create delete command for all shown (filtered) volumes</button>
    </div>
    <label class="hide-zero-cost-label" title="When on, rows with billed cost 0.00 are hidden">
        <input type="checkbox" id="hideZeroCostRows" aria-label="Hide resources with zero billed cost" checked />
        <span>Hide 0.00 cost</span>
    </label>
    </div>
    <div id="tableAllContainer" style="display: none;">
    <table id="resourcesTable">
        <thead>
            <tr>
"""

    for i, col in enumerate(display_cols):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "sortable resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "sortable created-date-col"
                if col == "OwnerCreatedDate"
                else ("sortable truncate-20-col" if col in truncate_cols else "sortable")
            )
        )
        html_content += f'                <th class="{th_class}" data-col="{i}">{html.escape(label)}</th>\n'

    html_content += """            </tr>
            <tr class="filter-row">
"""
    col_unique: dict[int, set[str]] = {i: set() for i in range(len(display_cols))}
    for row in rows:
        for i, col in enumerate(display_cols):
            if col == "ResourceName":
                val = (row.get("ResourceName") or "").strip() or (row.get("ResourceId") or "").strip()
            elif col == "MasterResource":
                val = (row.get("MasterResource") or "").strip()
            elif col == "OwnerCreatedDate":
                val = format_created_date_display(row.get(col, "") or "")
            else:
                val = row.get(col, "")
                if col == "BilledCost":
                    try:
                        cv = float(val or 0)
                        val = f"{to_display_cost(cv):.2f}"
                    except (ValueError, TypeError):
                        val = str(val)
            col_unique[i].add(str(val).strip() if val else "")

    EMPTY_SENTINEL = "__empty__"
    for i, col in enumerate(display_cols):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        unique_vals = sorted(col_unique[i], key=lambda v: (v == "", v.lower() if isinstance(v, str) else str(v)))
        opts = "".join(
            f'<option value="{html.escape(v) if v else EMPTY_SENTINEL}">{html.escape(v) if v else "(empty)"}</option>'
            for v in unique_vals
        )
        html_content += f'                <th><select data-col="{i}" aria-label="Filter {html.escape(label)}"><option value="__all__">(All)</option>{opts}</select></th>\n'

    html_content += """            </tr>
        </thead>
        <tbody>
"""

    for i, row in enumerate(rows):
        fd = rows_filter_data[i] if i < len(rows_filter_data) else {}
        unattached_attr = ' data-unattached-volume="1"' if fd.get("unattachedVolume") else ""
        tr_attrs = f' data-service-category="{html.escape(fd.get("serviceCategory", ""))}" data-compartment="{html.escape(fd.get("compartment", ""))}" data-region="{html.escape(fd.get("region", ""))}" data-age="{html.escape(fd.get("age", ""))}"{unattached_attr}'
        html_content += f"            <tr{tr_attrs}>\n"
        for col in display_cols:
            val = row.get(col, "")
            if col == "MasterResource":
                full_name = (val or "").strip() or "—"
                display_short = (full_name[:15] + "…") if len(full_name) > 15 else full_name
                tooltip = f' title="{html.escape(full_name)}"' if full_name else ""
                info_btn = (
                    combined_items_info_btn_html(full_name)
                    if full_name in multi_resource_masters
                    else ""
                )
                console_link = master_oci_console_link_html(row, oci_console_prefixes)
                deleted_icon = deleted_resource_icon_html(full_name)
                cell_content = f'{info_btn}{console_link}{deleted_icon}<span class="resource-name"{tooltip}>{html.escape(display_short)}</span>'
                html_content += f'                <td class="resource-name-col" data-value="{html.escape(full_name)}">{cell_content}</td>\n'
            elif col == "ResourceName":
                rid = (row.get("ResourceId") or "").strip()
                display = (val or "").strip() or rid or "—"
                display_short = (display[:15] + "…") if len(display) > 15 else display
                tooltip_text = rid or display
                tooltip = f' title="{html.escape(tooltip_text)}"' if tooltip_text else ""
                copy_target = rid or (display if display != "—" else "")
                copy_btn = (
                    f'<button type="button" class="copy-resource-btn" data-copy="{html.escape(copy_target)}" '
                    f'title="Copy ResourceId" aria-label="Copy ResourceId">⎘</button>'
                    if copy_target
                    else ""
                )
                console_link = oci_console_link_html(row, oci_console_prefixes)
                unattached_marker = (
                    unattached_volume_indicator_html()
                    if is_unattached_volume_row(row, unattached)
                    else ""
                )
                deleted_icon = deleted_resource_icon_html(display)
                cell_content = f'{console_link}{unattached_marker}{deleted_icon}<span class="resource-name"{tooltip}>{html.escape(display_short)}</span>{copy_btn}'
                html_content += f'                <td class="resource-name-col" data-value="{html.escape(display)}">{cell_content}</td>\n'
            elif col == "oci_CompartmentName":
                comp_name = (val or "").strip() or (row.get("oci_CompartmentPath") or "").strip() or "—"
                comp_path = (row.get("oci_CompartmentPath") or "").strip()
                tooltip = f' title="{html.escape(comp_path)}"' if comp_path else ""
                cell_content = f'<span class="compartment-cell"{tooltip}>{html.escape(comp_name)}</span>'
                html_content += f'                <td data-value="{html.escape(comp_name)}">{cell_content}</td>\n'
            elif col == "OwnerCreatedDate":
                display_val = format_created_date_display(val or "")
                html_content += f'                <td class="created-date-col" data-value="{html.escape(display_val or str(val))}">{html.escape(display_val or "—")}</td>\n'
            elif col in ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType"):
                full_text = (val or "").strip() or "—"
                display_short = (full_text[:20] + "…") if len(full_text) > 20 else full_text
                tooltip = f' title="{html.escape(full_text)}"'
                cell_content = f'<span class="truncate-cell"{tooltip}>{html.escape(display_short)}</span>'
                html_content += f'                <td class="truncate-20-col" data-value="{html.escape(full_text)}">{cell_content}</td>\n'
            else:
                if col == "BilledCost":
                    try:
                        cv = float(val or 0)
                        val = f"{to_display_cost(cv):.2f}"
                    except (ValueError, TypeError):
                        val = str(val)
                cell_class = "num" if col in ("BilledCost", "OwnerCreatedAgeDays") else ""
                html_content += f'                <td{" class=\"" + cell_class + "\"" if cell_class else ""} data-value="{html.escape(str(val))}">{html.escape(str(val))}</td>\n'
        html_content += "            </tr>\n"

    html_content += """        </tbody>
    </table>
    </div>
    <div id="tableGroupedContainer" style="display: none;">
    <table id="resourcesTableGrouped" class="grouped-table">
        <thead><tr><th class="sortable truncate-20-col" id="groupedTableLabel" data-col="0">Service Name</th><th class="sortable num" data-col="1">Count</th><th class="sortable num" data-col="2">Billed Cost</th></tr></thead>
        <tbody></tbody>
    </table>
    </div>
    <div id="tableGroupedMasterContainer">
    <table id="resourcesTableGroupedMaster" class="grouped-table">
        <thead>
            <tr>
"""
    for i, col in enumerate(display_cols):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "sortable resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "sortable created-date-col"
                if col == "OwnerCreatedDate"
                else (
                    "sortable num"
                    if col in ("BilledCost", "OwnerCreatedAgeDays")
                    else ("sortable truncate-20-col" if col in truncate_cols else "sortable")
                )
            )
        )
        html_content += f'                <th class="{th_class}" data-col="{i}">{html.escape(label)}</th>\n'
    html_content += """            </tr>
            <tr class="filter-row">
"""
    for i, col in enumerate(display_cols):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        unique_vals = sorted(col_unique[i], key=lambda v: (v == "", v.lower() if isinstance(v, str) else str(v)))
        opts = "".join(
            f'<option value="{html.escape(v) if v else EMPTY_SENTINEL}">{html.escape(v) if v else "(empty)"}</option>'
            for v in unique_vals
        )
        html_content += f'                <th><select data-col="{i}" aria-label="Filter {html.escape(label)}"><option value="__all__">(All)</option>{opts}</select></th>\n'
    html_content += """            </tr>
        </thead>
        <tbody></tbody>
    </table>
    </div>

    <script>
        (function() {
            const rowsFilterData = """ + json.dumps(rows_filter_data) + """;
            const currency = """ + json.dumps(currency) + """;
            const overallCostForPct = """ + str(overall_cost_for_pct) + """;
            const primaryCostLabel = """ + json.dumps(primary_label) + """;
            const primaryRankStr = """ + json.dumps(primary_rank_str) + """;
            const totalCostEl = document.getElementById('totalCost');
            const table = document.getElementById('resourcesTable');
            const tbody = table.querySelector('tbody');
            const headers = table.querySelectorAll('th.sortable');
            const filtersAll = Array.from(table.querySelectorAll('.filter-row select'));
            const chartFilterCat = document.getElementById('filterServiceCategory');
            const chartFilterComp = document.getElementById('filterCompartment');
            const chartFilterRegion = document.getElementById('filterRegion');
            const chartFilterAge = document.getElementById('filterAge');
            const BILLED_COST_COL = """ + str(display_cols.index("BilledCost")) + """;
            const SERVICE_NAME_COL = """ + str(display_cols.index("ServiceName")) + """;
            const MASTER_RESOURCE_COL = """ + str(display_cols.index("MasterResource")) + """;
            const RESOURCE_NAME_COL = """ + str(display_cols.index("ResourceName")) + """;
            let sortCol = BILLED_COST_COL, sortAsc = true;
            let groupedSortCol = 2, groupedSortAsc = false;
            let masterGroupedSortCol = BILLED_COST_COL, masterGroupedSortAsc = false;
            let tableMode = 'groupedMaster';
            const tableAllContainer = document.getElementById('tableAllContainer');
            const tableGroupedContainer = document.getElementById('tableGroupedContainer');
            const tableGroupedMasterContainer = document.getElementById('tableGroupedMasterContainer');
            const tableGroupedMaster = document.getElementById('resourcesTableGroupedMaster');
            const tbodyGroupedMaster = tableGroupedMaster?.querySelector('tbody');
            const filtersMaster = Array.from(tableGroupedMaster?.querySelectorAll('.filter-row select') || []);
            const tbodyGrouped = document.getElementById('resourcesTableGrouped')?.querySelector('tbody');
            const hideZeroCostEl = document.getElementById('hideZeroCostRows');
            const resourcesTableHeading = document.getElementById('resourcesTableHeading');
            const excluded = { cat: new Set(), comp: new Set(), region: new Set(), age: new Set() };

            function syncColumnFilters(fromList, toList) {
                fromList.forEach((f, i) => { if (toList[i]) toList[i].value = f.value; });
            }
            function usesMasterTable() {
                return tableMode === 'groupedMaster' || tableMode === 'unattachedVolumes';
            }
            function getColumnFilterVals() {
                const src = (usesMasterTable() && filtersMaster.length) ? filtersMaster : filtersAll;
                return src.map(f => f.value);
            }
            function onColumnFilterChange(source) {
                if (source === 'master') syncColumnFilters(filtersMaster, filtersAll);
                else syncColumnFilters(filtersAll, filtersMaster);
                applyAllFilters();
            }

            function isRoundedZeroCost(n) {
                return Number.isFinite(n) && Math.round(n * 100) / 100 === 0;
            }

            function createChart(canvasId, labels, values, colorArr, filterEl, excludeKey) {
                const canvas = document.getElementById(canvasId);
                const ctx = canvas.getContext('2d');
                const opts = {
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: { legend: { display: false }, tooltip: { enabled: true } }
                };
                if (filterEl || excludeKey) {
                    opts.onClick = (evt, elements, chart) => {
                        if (!elements.length) return;
                        const label = chart.data.labels[elements[0].index];
                        const isExclude = (evt && evt.shiftKey) || (evt && evt.native && evt.native.shiftKey);
                        if (isExclude && excludeKey) {
                            const s = excluded[excludeKey];
                            if (s.has(label)) s.delete(label); else s.add(label);
                            applyAllFilters();
                        } else if (filterEl) {
                            filterEl.value = label;
                            applyAllFilters();
                        }
                    };
                }
                const chart = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        labels: labels,
                        datasets: [{ data: values, backgroundColor: values.map((_, i) => colorArr[i % colorArr.length]), borderWidth: 0 }]
                    },
                    options: opts
                });
                if (excludeKey) {
                    canvas.addEventListener('click', (e) => {
                        if (e.shiftKey) {
                            const ch = Chart.getChart(canvas);
                            const el = ch?.getElementsAtEventForMode(e, 'nearest', { intersect: true }, false);
                            if (el && el.length && el[0].index !== undefined) {
                                const label = ch.data.labels[el[0].index];
                                const s = excluded[excludeKey];
                                if (s.has(label)) s.delete(label); else s.add(label);
                                applyAllFilters();
                                e.stopPropagation();
                                e.preventDefault();
                            }
                        }
                    }, true);
                }
                return chart;
            }
            const costChart = createChart('costChart', """ + json.dumps(labels) + """, """ + json.dumps(values) + """, """ + json.dumps(colors) + """, chartFilterCat, 'cat');
            const compChart = createChart('compartmentChart', """ + json.dumps(comp_labels) + """, """ + json.dumps(comp_values) + """, """ + json.dumps(comp_colors) + """, chartFilterComp, 'comp');
            const regionChart = createChart('regionChart', """ + json.dumps(region_labels) + """, """ + json.dumps(region_values) + """, """ + json.dumps(region_colors) + """, chartFilterRegion, 'region');
            const ageChart = createChart('ageChart', """ + json.dumps(age_labels) + """, """ + json.dumps(age_values) + """, """ + json.dumps(age_colors) + """, chartFilterAge, 'age');

            function updateChart(chart, data, colorArr) {
                chart.data.labels = data.map(([l]) => l);
                chart.data.datasets[0].data = data.map(([,c]) => c);
                chart.data.datasets[0].backgroundColor = Array.isArray(colorArr)
                    ? data.map((_, i) => colorArr[i % colorArr.length])
                    : data.map(([l]) => colorArr[l] || colorArr['(Unknown age)'] || '#94a3b8');
                chart.update();
            }

            function getCellValue(row, col) {
                const cell = row.cells[col];
                return (cell && cell.getAttribute('data-value')) || (cell ? cell.textContent.trim() : '');
            }

            function aggregateFiltered(filtered) {
                const byCat = {}, byComp = {}, byRegion = {}, byAge = {};
                let total = 0;
                filtered.forEach(r => {
                    const c = r.billedCost || 0;
                    total += c;
                    byCat[r.serviceCategory] = (byCat[r.serviceCategory] || 0) + c;
                    byComp[r.compartment] = (byComp[r.compartment] || 0) + c;
                    byRegion[r.region] = (byRegion[r.region] || 0) + c;
                    byAge[r.age] = (byAge[r.age] || 0) + c;
                });
                const ageOrder = ['<7 days','<31 days','<6 months','<1 year','1 to 2 years','2+ years','(Unknown age)'];
                return {
                    cat: Object.entries(byCat).sort((a,b)=>b[1]-a[1]),
                    comp: Object.entries(byComp).sort((a,b)=>b[1]-a[1]),
                    region: Object.entries(byRegion).sort((a,b)=>b[1]-a[1]),
                    age: Object.entries(byAge).sort((a,b)=>(ageOrder.indexOf(a[0])-ageOrder.indexOf(b[0]))||(b[1]-a[1])),
                    total
                };
            }

            const EMPTY_SENTINEL = '__empty__';
            const UNATTACHED_VOLUME_TOOLTIP = """ + json.dumps(UNATTACHED_VOLUME_TOOLTIP) + """;
            function unattachedVolumeIndicatorHtml() {
                return '<span class="unattached-volume-indicator" title="' + UNATTACHED_VOLUME_TOOLTIP.replace(/"/g, '&quot;') + '" aria-label="Unattached volume">!</span>';
            }
            function deleteVolumeCliBtnHtml(cmd) {
                if (!cmd) return '';
                const escCmd = String(cmd).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
                return '<button type="button" class="delete-volume-cli-btn" data-copy="' + escCmd + '" title="Copy OCI CLI delete command" aria-label="Copy OCI CLI delete command">🗑</button>';
            }
            function collectVisibleDeleteCommands(filteredRows) {
                const seen = new Set();
                const lines = [];
                (filteredRows || []).forEach(r => {
                    if (r.deleteCli && r.resourceId && !seen.has(r.resourceId)) {
                        seen.add(r.resourceId);
                        lines.push(r.deleteCli);
                    }
                });
                return lines;
            }
            let lastVisibleDeleteCommands = [];
            const bulkDeleteBtn = document.getElementById('copyFilteredDeleteCommandsBtn');
            function updateBulkDeleteButton(filteredRows) {
                if (!bulkDeleteBtn) return;
                const show = tableMode === 'unattachedVolumes';
                bulkDeleteBtn.style.display = show ? '' : 'none';
                if (!show) return;
                lastVisibleDeleteCommands = collectVisibleDeleteCommands(filteredRows);
                const n = lastVisibleDeleteCommands.length;
                bulkDeleteBtn.disabled = n === 0;
                bulkDeleteBtn.title = n
                    ? 'Copy ' + n + ' OCI CLI delete command(s) for currently visible volumes'
                    : 'No visible volumes to copy delete commands for';
            }
""" + get_combined_items_js_helpers() + """
            function relatedItemsForMaster(masterName) {
                let groupItems = (rowsFilterData || []).filter(r => (r.masterResource || '') === masterName);
                if (tableMode === 'unattachedVolumes') {
                    groupItems = groupItems.filter(r => r.unattachedVolume);
                }
                const masterItem = pickMasterItemForGroup(masterName, groupItems) || {};
                return buildCombinedRelatedItems(masterName, groupItems, masterItem || {});
            }
            function applyAllFilters() {
                updateDropdownStrikethrough();
                const catVal = chartFilterCat?.value || '';
                const compVal = chartFilterComp?.value || '';
                const regionVal = chartFilterRegion?.value || '';
                const ageVal = chartFilterAge?.value || '';
                const filtered = rowsFilterData.filter(r => {
                    if (excluded.cat.has(r.serviceCategory)) return false;
                    if (excluded.comp.has(r.compartment)) return false;
                    if (excluded.region.has(r.region)) return false;
                    if (excluded.age.has(r.age)) return false;
                    if (catVal && r.serviceCategory !== catVal) return false;
                    if (compVal && r.compartment !== compVal) return false;
                    if (regionVal && r.region !== regionVal) return false;
                    if (ageVal && r.age !== ageVal) return false;
                    if (tableMode === 'unattachedVolumes' && !r.unattachedVolume) return false;
                    return true;
                });
                const agg = aggregateFiltered(filtered);
                const pctStr = overallCostForPct > 0 ? ` (${(agg.total / overallCostForPct * 100).toFixed(1)}% of overall spend)` : '';
                totalCostEl.textContent = `${primaryCostLabel}: ${agg.total.toFixed(2)} ${currency}${pctStr}${primaryRankStr}`;
                updateChart(costChart, agg.cat, """ + json.dumps(colors) + """);
                updateChart(compChart, agg.comp, """ + json.dumps(comp_colors) + """);
                updateChart(regionChart, agg.region, """ + json.dumps(region_colors) + """);
                updateChart(ageChart, agg.age, """ + json.dumps(age_bucket_to_color) + """);
                const filterVals = getColumnFilterVals();
                const filterValsByCol = {};
                filterVals.forEach((v, col) => { filterValsByCol[col] = v; });
                const numCols = Object.keys(filterValsByCol).length;
                const hideZero = !!(hideZeroCostEl && hideZeroCostEl.checked);
                const rows = tbody.querySelectorAll('tr');
                function applyTableRowVisibility() {
                    rows.forEach((row) => {
                        const svcCat = row.getAttribute('data-service-category') || '';
                        const comp = row.getAttribute('data-compartment') || '';
                        const region = row.getAttribute('data-region') || '';
                        const age = row.getAttribute('data-age') || '';
                        let chartMatch = true;
                        if (excluded.cat.has(svcCat)) chartMatch = false;
                        if (excluded.comp.has(comp)) chartMatch = false;
                        if (excluded.region.has(region)) chartMatch = false;
                        if (excluded.age.has(age)) chartMatch = false;
                        if (catVal && svcCat !== catVal) chartMatch = false;
                        if (compVal && comp !== compVal) chartMatch = false;
                        if (regionVal && region !== regionVal) chartMatch = false;
                        if (ageVal && age !== ageVal) chartMatch = false;
                        if (tableMode === 'unattachedVolumes' && !row.hasAttribute('data-unattached-volume')) chartMatch = false;
                        let colMatch = true;
                        for (let j = 0; j < numCols; j++) {
                            const fv = filterValsByCol[j];
                            if (fv && fv !== '__all__') {
                                let cellVal = getCellValue(row, j);
                                cellVal = (cellVal === '' || cellVal == null) ? EMPTY_SENTINEL : cellVal;
                                const filterVal = fv === EMPTY_SENTINEL ? EMPTY_SENTINEL : fv;
                                if (cellVal !== filterVal) { colMatch = false; break; }
                            }
                        }
                        const rowCost = parseFloat(getCellValue(row, BILLED_COST_COL));
                        const zeroHidden = hideZero && isRoundedZeroCost(rowCost);
                        row.style.display = (chartMatch && colMatch && !zeroHidden) ? '' : 'none';
                    });
                }
                applyTableRowVisibility();
                const groupedModes = {
                    groupedService: { key: 'serviceName', label: 'Service Name' },
                    groupedResourceType: { key: 'resourceType', label: 'Resource Type' }
                };
                const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                if (usesMasterTable() && tbodyGroupedMaster) {
                    tableAllContainer.style.display = 'none';
                    tableGroupedContainer.style.display = 'none';
                    tableGroupedMasterContainer.style.display = '';
                    const hasTableCols = rowsFilterData.length && rowsFilterData[0].tableCols != null;
                    const filteredForMaster = hasTableCols
                        ? filtered.filter(r => {
                            for (let j = 0; j < filterVals.length; j++) {
                                if (filterVals[j] && filterVals[j] !== '__all__') {
                                    let cellVal = r.tableCols && r.tableCols[j] != null ? String(r.tableCols[j]) : '';
                                    cellVal = (cellVal === '' || cellVal == null) ? EMPTY_SENTINEL : cellVal;
                                    const fv = filterVals[j] === EMPTY_SENTINEL ? EMPTY_SENTINEL : filterVals[j];
                                    if (cellVal !== fv) return false;
                                }
                            }
                            return true;
                        })
                        : filtered;
                    const byGroup = {};
                    filteredForMaster.forEach(r => {
                        const key = r.masterResource || '(Uncategorized)';
                        if (!byGroup[key]) byGroup[key] = [];
                        byGroup[key].push(r);
                    });
                    let masterRows = Object.entries(byGroup).map(([masterName, items]) => {
                        const masterItem = pickMasterItemForGroup(masterName, items);
                        const cols = (masterItem.tableCols || []).slice();
                        const count = items.length;
                        const cost = items.reduce((s, x) => s + (x.billedCost || 0), 0);
                        const relatedItems = buildCombinedRelatedItems(masterName, items, masterItem);
                        if (cols.length) {
                            if (MASTER_RESOURCE_COL >= 0) cols[MASTER_RESOURCE_COL] = masterName;
                            if (RESOURCE_NAME_COL >= 0) cols[RESOURCE_NAME_COL] = masterItem.resourceName || masterName;
                            if (SERVICE_NAME_COL >= 0 && count > 1) cols[SERVICE_NAME_COL] = 'Combined ' + count + ' items';
                            if (BILLED_COST_COL >= 0) cols[BILLED_COST_COL] = cost.toFixed(2);
                        }
                        return {
                            masterName,
                            cols,
                            cost,
                            count,
                            relatedItems,
                            masterConsoleLink: masterItem.masterConsoleLink || '',
                            unattachedVolume: !!masterItem.unattachedVolume,
                            deleteCli: (() => {
                                const cmds = [...new Set(items.filter(x => x.deleteCli).map(x => x.deleteCli))];
                                return cmds.length === 1 ? cmds[0] : '';
                            })(),
                        };
                    });
                    if (hideZero) masterRows = masterRows.filter(r => !isRoundedZeroCost(r.cost));
                    const col = masterGroupedSortCol, asc = masterGroupedSortAsc;
                    masterRows.sort((a, b) => {
                        const va = (a.cols && a.cols[col] != null) ? a.cols[col] : '';
                        const vb = (b.cols && b.cols[col] != null) ? b.cols[col] : '';
                        const na = parseFloat(va), nb = parseFloat(vb);
                        let cmp = (!isNaN(na) && !isNaN(nb)) ? (na - nb) : String(va).localeCompare(String(vb), undefined, { numeric: true });
                        return asc ? cmp : -cmp;
                    });
                    document.querySelectorAll('#resourcesTableGroupedMaster th.sortable').forEach(h => h.classList.remove('sorted-asc', 'sorted-desc'));
                    const mh = document.querySelector('#resourcesTableGroupedMaster th.sortable[data-col="' + col + '"]');
                    if (mh) mh.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
                    const numCols = new Set([BILLED_COST_COL]);
                    tbodyGroupedMaster.innerHTML = masterRows.map(r => {
                        const tds = (r.cols || []).map((val, i) => {
                            const v = val == null ? '' : String(val);
                            const short = v.length > 20 ? (v.slice(0, 20) + '…') : v;
                            const cls = numCols.has(i) ? ' class="num"' : (i === MASTER_RESOURCE_COL ? ' class="resource-name-col"' : '');
                            let shown = numCols.has(i) && i === BILLED_COST_COL ? (esc(v) + ' ' + esc(currency)) : esc(short);
                            if (i === MASTER_RESOURCE_COL) {
                                let prefix = '';
                                if (r.count > 1) {
                                    prefix += '<button type="button" class="combined-items-info-btn" data-master="'
                                        + esc(r.masterName) + '" title="Show related resources" aria-label="Show related resources">i</button>';
                                }
                                if (r.masterConsoleLink) prefix += r.masterConsoleLink;
                                prefix += deletedResourceIconHtml(v);
                                shown = prefix + shown;
                            } else if (i === RESOURCE_NAME_COL) {
                                let prefix = '';
                                if (r.unattachedVolume) {
                                    prefix += unattachedVolumeIndicatorHtml();
                                }
                                if (tableMode === 'unattachedVolumes' && r.deleteCli) {
                                    prefix += deleteVolumeCliBtnHtml(r.deleteCli);
                                }
                                shown = prefix + deletedResourceIconHtml(v) + shown;
                            }
                            return '<td' + cls + ' data-value="' + esc(v) + '" title="' + esc(v) + '">' + shown + '</td>';
                        }).join('');
                        return '<tr>' + tds + '</tr>';
                    }).join('');
                } else if (groupedModes[tableMode] && tbodyGrouped) {
                    tableAllContainer.style.display = 'none';
                    tableGroupedContainer.style.display = '';
                    if (tableGroupedMasterContainer) tableGroupedMasterContainer.style.display = 'none';
                    const groupKey = groupedModes[tableMode].key;
                    const labelEl = document.getElementById('groupedTableLabel');
                    if (labelEl) labelEl.textContent = groupedModes[tableMode].label;
                    const byGroup = {};
                    filtered.forEach(r => {
                        const key = r[groupKey] || '(Uncategorized)';
                        if (!byGroup[key]) byGroup[key] = { cost: 0, count: 0 };
                        byGroup[key].cost += r.billedCost || 0;
                        byGroup[key].count += 1;
                    });
                    const groupedRows = Object.entries(byGroup);
                    const col = groupedSortCol, asc = groupedSortAsc;
                    groupedRows.sort((a, b) => {
                        let cmp;
                        if (col === 0) cmp = String(a[0]).localeCompare(String(b[0]), undefined, { numeric: true });
                        else if (col === 1) cmp = a[1].count - b[1].count;
                        else cmp = a[1].cost - b[1].cost;
                        return asc ? cmp : -cmp;
                    });
                    document.querySelectorAll('#resourcesTableGrouped th.sortable').forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                    if (document.querySelector('#resourcesTableGrouped th.sortable[data-col="' + col + '"]')) document.querySelector('#resourcesTableGrouped th.sortable[data-col="' + col + '"]').classList.add(asc ? 'sorted-asc' : 'sorted-desc');
                    const groupedToShow = hideZero
                        ? groupedRows.filter(([, v]) => !isRoundedZeroCost(v.cost))
                        : groupedRows;
                    tbodyGrouped.innerHTML = groupedToShow.map(([name, {cost, count}]) =>
                        `<tr><td class="truncate-20-col" data-value="${esc(name)}">${esc(name)}</td><td class="num" data-value="${count}">${count}</td><td class="num" data-value="${cost.toFixed(2)}">${cost.toFixed(2)} ${currency}</td></tr>`
                    ).join('');
                } else {
                    tableAllContainer.style.display = '';
                    tableGroupedContainer.style.display = 'none';
                    if (tableGroupedMasterContainer) tableGroupedMasterContainer.style.display = 'none';
                }
                if (resourcesTableHeading) {
                    resourcesTableHeading.textContent = tableMode === 'unattachedVolumes'
                        ? 'Volumes not Attached'
                        : 'All Resources';
                }
                updateBulkDeleteButton(filtered);
            }

            function applyFilters() { applyAllFilters(); }

            function sort(col) {
                sortAsc = (sortCol === col) ? !sortAsc : true;
                sortCol = col;
                headers.forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                headers[col]?.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.sort((a, b) => {
                    const va = getCellValue(a, col), vb = getCellValue(b, col);
                    const na = parseFloat(va), nb = parseFloat(vb);
                    let cmp = (!isNaN(na) && !isNaN(nb)) ? na - nb : String(va).localeCompare(String(vb), undefined, { numeric: true });
                    return sortAsc ? cmp : -cmp;
                });
                rows.forEach(r => tbody.appendChild(r));
            }

            const chartFilterLabels = { cat: """ + json.dumps(raw_labels) + """, comp: """ + json.dumps(raw_comp_labels) + """, region: """ + json.dumps(raw_region_labels) + """, age: """ + json.dumps(raw_age_labels) + """ };
            const chartFilterEls = { cat: chartFilterCat, comp: chartFilterComp, region: chartFilterRegion, age: chartFilterAge };
            function strikethrough(s) { return [...String(s)].map(c => c + '\\u0336').join(''); }
            function updateDropdownStrikethrough() {
                for (const [key, sel] of Object.entries(chartFilterEls)) {
                    if (!sel) continue;
                    const labels = chartFilterLabels[key] || [];
                    const excl = excluded[key];
                    const curVal = sel.value;
                    sel.innerHTML = '<option value="">(All)</option>' + labels.map(l => {
                        const esc = (x) => String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                        const text = excl.has(l) ? strikethrough(l) : l;
                        return '<option value="' + esc(l) + '">' + esc(text) + '</option>';
                    }).join('');
                    sel.value = curVal;
                }
            }

            tbody.addEventListener('click', (e) => {
                const btn = e.target.closest('.copy-resource-btn');
                if (btn && btn.getAttribute('data-copy')) {
                    navigator.clipboard.writeText(btn.getAttribute('data-copy')).then(() => {
                        const orig = btn.textContent;
                        btn.textContent = '✓';
                        setTimeout(() => { btn.textContent = orig; }, 800);
                    });
                }
            });
            headers.forEach((h, i) => h.addEventListener('click', () => sort(i)));
            filtersAll.forEach(f => f.addEventListener('change', () => onColumnFilterChange('all')));
            filtersMaster.forEach(f => f.addEventListener('change', () => onColumnFilterChange('master')));
            [chartFilterCat, chartFilterComp, chartFilterRegion, chartFilterAge].forEach(el => { if (el) el.addEventListener('change', applyAllFilters); });
            document.querySelectorAll('.reset-filter').forEach(btn => {
                btn.addEventListener('click', () => {
                    const key = btn.getAttribute('data-reset');
                    if (key && excluded[key]) excluded[key].clear();
                    const el = chartFilterEls[key];
                    if (el) el.value = '';
                    updateDropdownStrikethrough();
                    applyAllFilters();
                });
            });
            document.querySelectorAll('.reset-all-filters').forEach(btn => {
                btn.addEventListener('click', () => {
                    excluded.cat.clear(); excluded.comp.clear(); excluded.region.clear(); excluded.age.clear();
                    [chartFilterCat, chartFilterComp, chartFilterRegion, chartFilterAge].forEach(el => { if (el) el.value = ''; });
                    filtersAll.forEach(f => { f.value = '__all__'; });
                    filtersMaster.forEach(f => { f.value = '__all__'; });
                    updateDropdownStrikethrough();
                    applyAllFilters();
                });
            });
            if (hideZeroCostEl) hideZeroCostEl.addEventListener('change', applyAllFilters);
            document.addEventListener('click', (e) => {
                const btn = e.target.closest('.delete-volume-cli-btn');
                if (btn && btn.getAttribute('data-copy')) {
                    navigator.clipboard.writeText(btn.getAttribute('data-copy')).then(() => {
                        const orig = btn.textContent;
                        btn.textContent = '✓';
                        setTimeout(() => { btn.textContent = orig; }, 800);
                    });
                }
            });
            if (bulkDeleteBtn) {
                bulkDeleteBtn.addEventListener('click', () => {
                    const lines = lastVisibleDeleteCommands;
                    if (!lines.length) return;
                    navigator.clipboard.writeText(lines.join('\\n') + '\\n').then(() => {
                        const orig = bulkDeleteBtn.textContent;
                        bulkDeleteBtn.textContent = 'Copied ' + lines.length + ' delete command(s) ✓';
                        setTimeout(() => { bulkDeleteBtn.textContent = orig; }, 2000);
                    });
                });
            }
            document.querySelectorAll('.table-toggle-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const mode = btn.getAttribute('data-mode');
                    if (!mode) return;
                    const prevMode = tableMode;
                    const prevUsesMaster = prevMode === 'groupedMaster' || prevMode === 'unattachedVolumes';
                    const modeUsesMaster = mode === 'groupedMaster' || mode === 'unattachedVolumes';
                    if (prevUsesMaster && !modeUsesMaster) syncColumnFilters(filtersMaster, filtersAll);
                    if (modeUsesMaster && !prevUsesMaster) syncColumnFilters(filtersAll, filtersMaster);
                    tableMode = mode;
                    document.querySelectorAll('.table-toggle-btn').forEach(b => {
                        b.classList.toggle('active', b.getAttribute('data-mode') === mode);
                        b.setAttribute('aria-pressed', b.getAttribute('data-mode') === mode);
                    });
                    applyAllFilters();
                });
            });
            document.querySelectorAll('#resourcesTableGrouped th.sortable').forEach(th => {
                th.addEventListener('click', () => {
                    const col = parseInt(th.getAttribute('data-col'), 10);
                    if (isNaN(col)) return;
                    groupedSortAsc = (groupedSortCol === col) ? !groupedSortAsc : true;
                    groupedSortCol = col;
                    if (tableMode === 'groupedService' || tableMode === 'groupedResourceType') applyAllFilters();
                });
            });
            document.querySelectorAll('#resourcesTableGroupedMaster th.sortable').forEach(th => {
                th.addEventListener('click', () => {
                    const col = parseInt(th.getAttribute('data-col'), 10);
                    if (isNaN(col)) return;
                    masterGroupedSortAsc = (masterGroupedSortCol === col) ? !masterGroupedSortAsc : true;
                    masterGroupedSortCol = col;
                    if (usesMasterTable()) applyAllFilters();
                });
            });
            applyAllFilters();

            const costPeriodSwitch = document.getElementById('costPeriodSwitch');
            const alternateHref = """ + json.dumps(alternate_href) + """;
            const is30DayPage = """ + json.dumps(is_30day) + """;
            if (costPeriodSwitch) {
                costPeriodSwitch.addEventListener('change', () => {
                    const want30 = costPeriodSwitch.checked;
                    if (want30 !== is30DayPage) {
                        window.location.href = alternateHref + (window.location.search || '');
                    }
                });
            }
        })();
    </script>
    </div>
"""
    html_content += f"""{get_filter_help_modal_html()}
{get_combined_items_modal_html()}
    <script>
{get_filter_help_modal_init_js()}
    </script>
</body>
</html>
"""

    output_path.write_text(html_content, encoding="utf-8")


def generate_index_html(
    by_owner: dict[str, list[dict]],
    date_str: str,
    output_path: Path,
    consolidated_dir: Path | None = None,
    owner_30day_totals: dict[str, float] | None = None,
    total_30day_overall: float = 0,
) -> None:
    """Generate index page listing all users with links to their dashboards."""
    currency = "USD" if get_usd_conversion_rate() is not None else "USD"
    if consolidated_dir is None:
        consolidated_dir = output_path.parent.parent
    monthly_report = find_latest_monthly_report(consolidated_dir) or "2026/2026-03.html"
    top_nav = get_top_nav_html("../", monthly_report)
    owner_30day = owner_30day_totals or {}

    # Sort by total cost descending (default: last 30 days)
    user_data: list[tuple[str, float, float, int]] = []
    for owner, rows in by_owner.items():
        total = 0
        for r in rows:
            try:
                total += to_display_cost(float(r.get("BilledCost") or 0))
            except (ValueError, TypeError):
                pass
        total_30d = owner_30day.get(owner, 0.0)
        user_data.append((owner, total, total_30d, len(rows)))
    user_data.sort(key=lambda x: -x[2])

    rows_html = ""
    for owner, total_cost, total_30d, count in user_data:
        safe_name = sanitize_owner_filename(owner)
        rows_html += (
            f'        <tr data-owner="{html.escape(owner)}" data-count="{count}" '
            f'data-cost="{total_cost:.15f}" data-cost-30d="{total_30d:.15f}">'
            f'<td><a class="user-link" href="{html.escape(safe_name)}_30d.html" '
            f'data-href-day="{html.escape(safe_name)}.html" '
            f'data-href-30d="{html.escape(safe_name)}_30d.html">{html.escape(owner)}</a></td>'
            f'<td class="num">{count}</td>'
            f'<td class="num cost-cell">{total_30d:.2f} {currency}</td></tr>\n'
        )

    total_all = sum(t for _, t, _, _ in user_data)
    total_all_30d = sum(t for _, _, t, _ in user_data) if owner_30day else total_30day_overall
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Per-User Dashboards</title>
    <style>
        :root {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
        body {{ margin: 0; padding: 0; background: #f8fafc; color: #1e293b; }}
        {get_top_nav_css()}
        .report-content {{ padding: 2rem; }}
        h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; color: #0f172a; }}
        .subtitle {{ color: #64748b; margin-bottom: 2rem; }}
        .total-cost {{ font-size: 1.25rem; font-weight: 600; color: #0c4a6e; margin-bottom: 1rem; }}
        .search-box {{ margin-bottom: 1rem; }}
        .search-box input {{ width: 100%; max-width: 400px; padding: 0.5rem 0.75rem; font-size: 1rem; border: 1px solid #e2e8f0; border-radius: 6px; }}
        .search-box input:focus {{ outline: none; border-color: #0f172a; }}
        .search-box input::placeholder {{ color: #94a3b8; }}
        table {{ width: 100%; max-width: 800px; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        th {{ background: #0f172a; color: white; padding: 0.75rem 1rem; text-align: left; font-weight: 500; }}
        th.sortable {{ cursor: pointer; user-select: none; }}
        th.sortable:hover {{ background: #1e293b; }}
        th.sorted-asc::after {{ content: ' \\25B2'; font-size: 0.7em; opacity: 0.8; }}
        th.sorted-desc::after {{ content: ' \\25BC'; font-size: 0.7em; opacity: 0.8; }}
        td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }}
        tr:hover {{ background: #f8fafc; }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        a {{ color: #0f172a; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .index-header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.5rem; }}
        .cost-period-toggle {{ display: inline-flex; align-items: center; gap: 0.65rem; padding: 0.45rem 0.75rem; background: white; border: 1px solid #e2e8f0; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); flex-shrink: 0; }}
        .cost-period-label {{ font-size: 0.875rem; color: #64748b; user-select: none; }}
        .cost-period-label.active {{ color: #0f172a; font-weight: 600; }}
        .cost-period-switch {{ position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }}
        .cost-period-switch input {{ opacity: 0; width: 0; height: 0; }}
        .cost-period-slider {{ position: absolute; cursor: pointer; inset: 0; background: #cbd5e1; border-radius: 999px; transition: background 0.2s; }}
        .cost-period-slider::before {{ position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: transform 0.2s; box-shadow: 0 1px 2px rgba(0,0,0,0.15); }}
        .cost-period-switch input:checked + .cost-period-slider {{ background: #0f172a; }}
        .cost-period-switch input:checked + .cost-period-slider::before {{ transform: translateX(20px); }}
    </style>
</head>
<body>
{top_nav}
    <div class="report-content">
    <div class="index-header">
        <div>
            <h1>Per-User Dashboards</h1>
            <p class="subtitle" id="indexSubtitle">Costs for the last 30 days ending {date_str} · {len(by_owner)} users</p>
        </div>
        <div class="cost-period-toggle" title="Switch between last day and last 30 days cost view">
            <span class="cost-period-label" id="costPeriodLabelDay">Last day</span>
            <label class="cost-period-switch" aria-label="Switch cost period">
                <input type="checkbox" id="costPeriodSwitch" checked />
                <span class="cost-period-slider"></span>
            </label>
            <span class="cost-period-label active" id="costPeriodLabel30d">Last 30 days</span>
        </div>
    </div>
    <div class="total-cost" id="totalCostAll">Total cost across all users: {total_all_30d:.2f} {currency}</div>
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="Search users (contains, case-insensitive)" aria-label="Search users">
    </div>
    <table id="usersTable">
        <thead><tr><th class="sortable" data-col="0">Owner</th><th class="sortable num" data-col="1">Resources</th><th class="sortable num sorted-desc" data-col="2" id="costColumnHeader">Cost (last 30 days)</th></tr></thead>
        <tbody>
{rows_html}        </tbody>
    </table>
    <script>
        (function() {{
            const table = document.getElementById('usersTable');
            const tbody = table.querySelector('tbody');
            const searchInput = document.getElementById('searchInput');
            const headers = table.querySelectorAll('th.sortable');
            const costPeriodSwitch = document.getElementById('costPeriodSwitch');
            const costPeriodLabelDay = document.getElementById('costPeriodLabelDay');
            const costPeriodLabel30d = document.getElementById('costPeriodLabel30d');
            const totalCostAllEl = document.getElementById('totalCostAll');
            const indexSubtitleEl = document.getElementById('indexSubtitle');
            const costColumnHeader = document.getElementById('costColumnHeader');
            const currency = {json.dumps(currency)};
            const totalDay = {total_all};
            const total30d = {total_all_30d};
            const dateStr = {json.dumps(date_str)};
            const userCount = {len(by_owner)};
            let sortCol = 2, sortAsc = false;
            let is30Day = true;

            function activeCostAttr() {{
                return is30Day ? 'data-cost-30d' : 'data-cost';
            }}

            function updateCostPeriodView() {{
                is30Day = !!(costPeriodSwitch && costPeriodSwitch.checked);
                costPeriodLabelDay?.classList.toggle('active', !is30Day);
                costPeriodLabel30d?.classList.toggle('active', is30Day);
                if (costColumnHeader) {{
                    costColumnHeader.textContent = is30Day ? 'Cost (last 30 days)' : 'Cost (last day)';
                }}
                if (indexSubtitleEl) {{
                    indexSubtitleEl.textContent = is30Day
                        ? `Costs for the last 30 days ending ${{dateStr}} · ${{userCount}} users`
                        : `Based on consolidated report dated ${{dateStr}} (most recent) · ${{userCount}} users`;
                }}
                if (totalCostAllEl) {{
                    const total = is30Day ? total30d : totalDay;
                    totalCostAllEl.textContent = `Total cost across all users: ${{total.toFixed(2)}} ${{currency}}`;
                }}
                tbody.querySelectorAll('tr').forEach(row => {{
                    const cost = parseFloat(row.getAttribute(activeCostAttr()) || '0') || 0;
                    const cell = row.querySelector('.cost-cell');
                    if (cell) cell.textContent = `${{cost.toFixed(2)}} ${{currency}}`;
                    row.querySelectorAll('a.user-link').forEach(link => {{
                        const href = is30Day
                            ? link.getAttribute('data-href-30d')
                            : link.getAttribute('data-href-day');
                        if (href) link.setAttribute('href', href);
                    }});
                }});
                applyFilterAndSort();
            }}

            function applyFilterAndSort() {{
                const search = (searchInput.value || '').trim().toLowerCase();
                const costAttr = activeCostAttr();
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.forEach(row => {{
                    const owner = (row.getAttribute('data-owner') || '').toLowerCase();
                    const match = !search || owner.includes(search);
                    row.style.display = match ? '' : 'none';
                }});
                const visible = rows.filter(r => r.style.display !== 'none');
                visible.sort((a, b) => {{
                    const va = a.getAttribute(costAttr);
                    const vb = b.getAttribute(costAttr);
                    const oa = a.getAttribute('data-owner') || '';
                    const ob = b.getAttribute('data-owner') || '';
                    const ca = parseInt(a.getAttribute('data-count') || '0', 10);
                    const cb = parseInt(b.getAttribute('data-count') || '0', 10);
                    let cmp;
                    if (sortCol === 0) cmp = oa.localeCompare(ob, undefined, {{ numeric: true }});
                    else if (sortCol === 1) cmp = ca - cb;
                    else cmp = (parseFloat(va) || 0) - (parseFloat(vb) || 0);
                    return sortAsc ? cmp : -cmp;
                }});
                visible.forEach(r => tbody.appendChild(r));
            }}

            function sort(col) {{
                sortAsc = (sortCol === col) ? !sortAsc : (col === 2 ? false : true);
                sortCol = col;
                headers.forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                headers[col]?.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
                applyFilterAndSort();
            }}

            headers.forEach((h, i) => h.addEventListener('click', () => sort(i)));
            searchInput.addEventListener('input', applyFilterAndSort);
            if (costPeriodSwitch) costPeriodSwitch.addEventListener('change', updateCostPeriodView);
            updateCostPeriodView();
        }})();
    </script>
    </div>
</body>
</html>
"""

    output_path.write_text(html_content, encoding="utf-8")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    consolidated_csv_dir = script_dir / "FocusReports" / "ConsolidatedCSV"
    consolidated_html_dir = script_dir / "FocusReports" / "ConsolidatedHTML"
    users_dir = consolidated_html_dir / "users"

    result = find_most_recent_daily_report(consolidated_csv_dir)
    if not result:
        print("No daily consolidated report found under FocusReports/ConsolidatedCSV")
        return

    _, most_recent_date_str = result
    day_margin = get_day_margin()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", most_recent_date_str)
    if not m:
        print("Invalid date format from most recent report")
        return
    most_recent_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    report_date = most_recent_date - timedelta(days=day_margin)
    date_str = report_date.strftime("%Y-%m-%d")

    csv_path = find_daily_report_for_date(consolidated_csv_dir, date_str)
    if not csv_path:
        print(f"Report for {date_str} (DayMargin={day_margin} day(s) before {most_recent_date_str}) not found. Using most recent.")
        csv_path, _ = result
        date_str = most_recent_date_str
    else:
        print(f"Using report: {csv_path.name} ({date_str}, {day_margin} day(s) before most recent {most_recent_date_str})")

    rows = load_consolidated_csv(csv_path)
    if not rows:
        print("Report is empty")
        return

    by_owner = group_rows_by_owner(rows)
    users_dir.mkdir(parents=True, exist_ok=True)

    storage_json = script_dir / "consolidateReports" / "StorageReport" / "unattached_volumes.json"
    unattached_ocids = load_unattached_volume_ocid_set(storage_json)
    relations_json = script_dir / "relationships" / "oci-resource-relations.json"
    names_json = script_dir / "relationships" / "oci-resource-names.json"
    names_lookup = load_resource_names_lookup(names_json)
    master_lookup, master_id_lookup, master_type_lookup, master_charge_lookup = (
        load_resource_master_lookups(relations_json, names_lookup=names_lookup)
    )

    # Total cost for the day (all users)
    total_day_cost = sum(
        to_display_cost(float(r.get("BilledCost") or 0))
        for r in rows
    )

    # Compute daily cost and 30-day cost per owner for ranking (one scan of last-30-days CSVs)
    owner_daily_costs = {
        o: sum(to_display_cost(float(r.get("BilledCost") or 0)) for r in owner_rows)
        for o, owner_rows in by_owner.items()
    }
    total_30day_overall, owner_daily_cost_data = build_30day_rollup_for_owners(
        consolidated_csv_dir, date_str, set(by_owner.keys())
    )
    owner_30day_totals = {
        o: sum(sum((d.get("services") or {}).values()) for d in owner_daily_cost_data.get(o, []))
        for o in by_owner
    }

    # Generate index (after 30-day totals so the slider can show both periods)
    index_path = users_dir / "index.html"
    generate_index_html(
        by_owner,
        date_str,
        index_path,
        consolidated_html_dir,
        owner_30day_totals=owner_30day_totals,
        total_30day_overall=total_30day_overall,
    )
    print(f"  index.html: {len(by_owner)} users")

    sorted_daily = sorted(owner_daily_costs.items(), key=lambda x: -x[1])
    rank_daily = {owner: i + 1 for i, (owner, _) in enumerate(sorted_daily)}
    sorted_30day = sorted(owner_30day_totals.items(), key=lambda x: -x[1])
    rank_30day = {owner: i + 1 for i, (owner, _) in enumerate(sorted_30day)}
    total_owners = len(by_owner)

    # Generate per-user dashboards (last day + last 30 days)
    for owner, owner_rows in by_owner.items():
        safe_name = sanitize_owner_filename(owner)
        daily_cost_data = owner_daily_cost_data[owner]
        common_kwargs = dict(
            owner=owner,
            date_str=date_str,
            total_day_cost=total_day_cost,
            daily_cost_data=daily_cost_data,
            total_30day_overall=total_30day_overall,
            consolidated_dir=consolidated_html_dir,
            rank_daily=rank_daily.get(owner, 0),
            rank_30day=rank_30day.get(owner, 0),
            total_owners=total_owners,
            unattached_volume_ocids=unattached_ocids,
            master_resource_lookup=master_lookup,
            master_resource_id_lookup=master_id_lookup,
            master_resource_type_lookup=master_type_lookup,
            master_resource_charge_lookup=master_charge_lookup,
            resource_names_lookup=names_lookup,
            daily_owner_cost=owner_daily_costs.get(owner, 0),
        )
        out_path_day = users_dir / f"{safe_name}.html"
        generate_per_user_html(
            owner_rows,
            output_path=out_path_day,
            cost_mode="day",
            **common_kwargs,
        )
        rows_30d = build_owner_30day_resource_rows(consolidated_csv_dir, date_str, owner)
        out_path_30d = users_dir / f"{safe_name}_30d.html"
        generate_per_user_html(
            rows_30d,
            output_path=out_path_30d,
            cost_mode="30day",
            **common_kwargs,
        )
        print(f"  {safe_name}.html: {len(owner_rows)} resources (day), {len(rows_30d)} resources (30d)")

    print("-" * 60)
    print(f"Done. Open {users_dir / 'index.html'} to view.")


if __name__ == "__main__":
    main()
