"""
Build the unattached-volumes HTML dashboard from JSON written by getStorageReport.py.

No OCI calls — reads consolidateReports/StorageReport/unattached_volumes.json by default.
Optional deletion-schedule defined tags are applied during getStorageReport.py when
SetDeletionForUnattachedDisk is enabled in config.json (not in this script).
Table includes Cost (30d) from billed_cost_last_30_days when present.
"""

import argparse
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

from consolidateReports import (
    _age_bucket,
    load_resource_type_console_prefixes,
    oci_console_link_html,
    region_for_oci_console,
)
from reportNav import (
    find_latest_monthly_report,
    get_filter_help_modal_css,
    get_filter_help_modal_html,
    get_filter_help_modal_init_js,
    get_filter_help_nav_icon_html,
    get_top_nav_css,
    get_top_nav_html,
)

STORAGE_JSON_DEFAULT = "unattached_volumes.json"


def volume_delete_cli_command(ocid: str, billing_region: str, kind: str) -> str:
    """OCI CLI command to delete an unattached block or boot volume."""
    region = region_for_oci_console(ocid, billing_region)
    if not ocid or not region:
        return ""
    if kind == "boot":
        return f"oci bv boot-volume delete --boot-volume-id {ocid} --region {region} --force"
    return f"oci bv volume delete --volume-id {ocid} --region {region} --force"


def console_link_for_volume(volume_ocid: str, region: str, kind: str, prefixes: dict) -> str:
    resource_type = "bootvolume" if kind == "boot" else "volume"
    fake_row = {
        "ServiceName": "block_storage",
        "ServiceCategory": "",
        "ResourceType": resource_type,
        "ResourceId": volume_ocid,
        "Region": region,
        "ResourceName": "",
    }
    return oci_console_link_html(fake_row, prefixes)


def _aggregate_gb_per_owner(rows: list[dict]) -> list[tuple[str, float]]:
    """Sum size (GB) per owner tag; empty owner -> (empty). Sorted by GB descending."""
    owner_gb: defaultdict[str, float] = defaultdict(float)
    for r in rows:
        ok = (r.get("owner") or "").strip() or "(empty)"
        try:
            owner_gb[ok] += float(r.get("size_in_gbs") or 0)
        except (ValueError, TypeError):
            pass
    return sorted(owner_gb.items(), key=lambda x: -x[1])


def _aggregate_gb_per_region(rows: list[dict]) -> list[tuple[str, float]]:
    """Sum size (GB) per region; empty -> (empty). Sorted by GB descending."""
    by_region: defaultdict[str, float] = defaultdict(float)
    for r in rows:
        reg = (r.get("region") or "").strip() or "(empty)"
        try:
            by_region[reg] += float(r.get("size_in_gbs") or 0)
        except (ValueError, TypeError):
            pass
    return sorted(by_region.items(), key=lambda x: -x[1])


def _aggregate_gb_per_age_bucket(rows: list[dict]) -> list[tuple[str, float]]:
    """Sum size (GB) per age bucket (same buckets as per-user / consolidateReports)."""
    order = [
        "<7 days",
        "<31 days",
        "<6 months",
        "<1 year",
        "1 to 2 years",
        "2+ years",
        "(Unknown age)",
    ]
    by_age: defaultdict[str, float] = defaultdict(float)
    for r in rows:
        bucket = _age_bucket(r.get("age_days") or "")
        try:
            by_age[bucket] += float(r.get("size_in_gbs") or 0)
        except (ValueError, TypeError):
            pass
    return sorted(
        by_age.items(),
        key=lambda x: (order.index(x[0]) if x[0] in order else 999, -x[1]),
    )


# Same palette mapping as perUserReport / consolidateReports age chart
_AGE_BUCKET_TO_COLOR = {
    "<7 days": "#4ade80",
    "<31 days": "#22c55e",
    "<6 months": "#166534",
    "<1 year": "#b91c1c",
    "1 to 2 years": "#c62828",
    "2+ years": "#dc2626",
    "(Unknown age)": "#94a3b8",
}


def _cost_display_and_value(raw) -> tuple[str, str]:
    """Display text and data-value for billed_cost_last_30_days (null → em dash + empty for filters)."""
    if raw is None:
        return "—", ""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return "—", ""
    disp = f"{f:.2f}"
    return disp, disp


def generate_storage_html(
    rows: list[dict],
    output_path: Path,
    generated_label: str,
    consolidated_dir: Path,
    cost_subtitle: str = "",
) -> None:
    """Write unattached-volumes dashboard HTML (same nav shell as per-user dashboards)."""
    monthly_report = find_latest_monthly_report(consolidated_dir) or "2026/2026-03.html"
    top_nav = get_top_nav_html("", monthly_report)
    prefixes = load_resource_type_console_prefixes()

    total_gb = 0.0
    total_cost_all = 0.0
    for r in rows:
        try:
            total_gb += float(r.get("size_in_gbs") or 0)
        except (ValueError, TypeError):
            pass
        raw_cost = r.get("billed_cost_last_30_days")
        if raw_cost is not None:
            try:
                total_cost_all += float(raw_cost)
            except (ValueError, TypeError):
                pass

    owner_pairs = _aggregate_gb_per_owner(rows)
    owner_pairs_pos = [(o, round(g, 2)) for o, g in owner_pairs if g > 0]
    chart_labels = [p[0] for p in owner_pairs_pos]
    chart_values = [p[1] for p in owner_pairs_pos]
    chart_colors = [
        "#1a365d",
        "#2c5282",
        "#2b6cb0",
        "#3182ce",
        "#4299e1",
        "#63b3ed",
        "#065f46",
        "#047857",
        "#059669",
        "#581c87",
        "#6b21a8",
        "#9333ea",
    ]
    chart_bg = [chart_colors[i % len(chart_colors)] for i in range(len(chart_labels))]

    age_pairs = _aggregate_gb_per_age_bucket(rows)
    age_pairs_pos = [(b, round(g, 2)) for b, g in age_pairs if g > 0]
    age_labels = [p[0] for p in age_pairs_pos]
    age_values = [p[1] for p in age_pairs_pos]
    age_bg = [_AGE_BUCKET_TO_COLOR.get(lbl, "#94a3b8") for lbl in age_labels]

    region_pairs = _aggregate_gb_per_region(rows)
    region_pairs_pos = [(reg, round(g, 2)) for reg, g in region_pairs if g > 0]
    region_labels = [p[0] for p in region_pairs_pos]
    region_values = [p[1] for p in region_pairs_pos]
    region_bg = [chart_colors[i % len(chart_colors)] for i in range(len(region_labels))]

    def _dropdown_opts_chart(items: list[tuple[str, float]]) -> str:
        opts = '<option value="">(All)</option>'
        for label, _ in items:
            opts += f'<option value="{html.escape(label)}">{html.escape(label)}</option>'
        return opts

    owner_dropdown = _dropdown_opts_chart(owner_pairs_pos)
    region_dropdown = _dropdown_opts_chart(region_pairs_pos)
    age_dropdown = _dropdown_opts_chart(age_pairs_pos)
    raw_owner_labels = [p[0] for p in owner_pairs_pos]
    raw_region_labels = [p[0] for p in region_pairs_pos]
    raw_age_labels = [p[0] for p in age_pairs_pos]

    charts_row_html = ""
    if chart_labels or region_labels or age_labels:
        owner_block = ""
        if chart_labels:
            owner_block = f"""
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin:0;font-size:1rem;">Owner</h2><span style="display:flex;align-items:center;gap:0.25rem;"><span class="treemap-toggle" data-treemap="treemapSectionStorageOwner" title="Show treemap">▦</span><span class="reset-filter" data-reset="owner" title="Reset Owner filter">↺</span></span></div>
                <canvas id="ownerChart" width="400" height="280" aria-label="Unattached capacity by owner"></canvas>
                <div class="chart-filter"><select id="filterOwner" aria-label="Filter by Owner">{owner_dropdown}</select></div>
            </div>
        </div>"""
        region_block = ""
        if region_labels:
            region_block = f"""
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin:0;font-size:1rem;">Region</h2><span style="display:flex;align-items:center;gap:0.25rem;"><span class="treemap-toggle" data-treemap="treemapSectionStorageRegion" title="Show treemap">▦</span><span class="reset-filter" data-reset="region" title="Reset Region filter">↺</span></span></div>
                <canvas id="regionChart" width="400" height="280" aria-label="Unattached capacity by region"></canvas>
                <div class="chart-filter"><select id="filterRegion" aria-label="Filter by Region">{region_dropdown}</select></div>
            </div>
        </div>"""
        age_block = ""
        if age_labels:
            age_block = f"""
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin:0;font-size:1rem;">Age</h2><span style="display:flex;align-items:center;gap:0.25rem;"><span class="treemap-toggle" data-treemap="treemapSectionStorageAge" title="Show treemap">▦</span><span class="reset-filter" data-reset="age" title="Reset Age filter">↺</span></span></div>
                <canvas id="ageChart" width="400" height="280" aria-label="Unattached capacity by age bucket"></canvas>
                <div class="chart-filter"><select id="filterAge" aria-label="Filter by Age">{age_dropdown}</select></div>
            </div>
        </div>"""
        charts_row_html = f"""
        <div class="charts-row">
{owner_block}
{region_block}
{age_block}
        </div>
"""
        treemap_section_parts: list[str] = []
        if chart_labels:
            treemap_section_parts.append(
                """        <div class="treemap-section" id="treemapSectionStorageOwner">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Owner</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapStorageOwner"></div></div>
        </div>"""
            )
        if region_labels:
            treemap_section_parts.append(
                """        <div class="treemap-section" id="treemapSectionStorageRegion">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Region</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapStorageRegion"></div></div>
        </div>"""
            )
        if age_labels:
            treemap_section_parts.append(
                """        <div class="treemap-section" id="treemapSectionStorageAge">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Age</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapStorageAge"></div></div>
        </div>"""
            )
        treemap_sections_html = (
            "    <div id=\"treemapsBelowCharts\" style=\"margin-top: 2rem;\">\n"
            + "\n".join(treemap_section_parts)
            + "\n    </div>"
            if treemap_section_parts
            else ""
        )
    else:
        treemap_sections_html = ""

    # Table columns (filter dropdowns use same data-value strings as cells; empty -> __empty__ in JS)
    storage_col_labels = [
        "OCID",
        "Region",
        "Compartment",
        "Kind",
        "Display name",
        "State",
        "Size (GB)",
        "Cost (30d)",
        "Owner",
        "Created",
        "Age (days)",
    ]
    empty_sentinel = "__empty__"
    col_unique: dict[int, set[str]] = {i: set() for i in range(len(storage_col_labels))}

    rows_filter_data: list[dict] = []
    tbody_html = ""
    for r in rows:
        region = (r.get("region") or "").strip()
        region_norm = region or "(empty)"
        comp = (r.get("compartment_path") or "").strip()
        kind = (r.get("volume_kind") or "").strip()
        kind_norm = kind or "(empty)"
        disp = (r.get("display_name") or "").strip()
        ocid = (r.get("ocid") or "").strip()
        st = (r.get("lifecycle_state") or "").strip()
        owner = (r.get("owner") or "").strip()
        owner_norm = owner or "(empty)"
        age_bucket = _age_bucket(r.get("age_days") or "")
        created_disp = (r.get("created_display") or "").strip()
        age_s = (r.get("age_days") or "").strip()
        age_sort = age_s if age_s.isdigit() else "-1"
        try:
            sz = float(r.get("size_in_gbs") or 0)
            sz_s = f"{sz:.0f}" if sz == int(sz) else f"{sz:g}"
        except (ValueError, TypeError):
            sz = 0.0
            sz_s = str(r.get("size_in_gbs") or "")
        cost_show, cost_dv = _cost_display_and_value(r.get("billed_cost_last_30_days"))
        rows_filter_data.append(
            {
                "owner": owner_norm,
                "region": region_norm,
                "age": age_bucket,
                "kind": kind_norm,
                "sizeGb": sz,
            }
        )
        col_unique[0].add(ocid)
        col_unique[1].add(region)
        col_unique[2].add(comp)
        col_unique[3].add(kind)
        col_unique[4].add(disp)
        col_unique[5].add(st)
        col_unique[6].add(sz_s)
        col_unique[7].add(cost_dv)
        col_unique[8].add(owner)
        col_unique[9].add(created_disp)
        col_unique[10].add(age_sort)

        link = console_link_for_volume(ocid, region, kind, prefixes)
        ocid_show = ocid if len(ocid) <= 15 else (ocid[:15] + "…")
        copy_btn = (
            f'<button type="button" class="copy-ocid-btn" data-copy="{html.escape(ocid)}" title="Copy full OCID">⎘</button>'
            if ocid
            else ""
        )
        delete_cli = volume_delete_cli_command(ocid, region, kind)
        delete_cli_attr = (
            f' data-delete-cli="{html.escape(delete_cli)}"' if delete_cli else ""
        )
        delete_btn = (
            f'<button type="button" class="delete-volume-cli-btn" data-copy="{html.escape(delete_cli)}" '
            f'title="Copy OCI CLI delete command" aria-label="Copy OCI CLI delete command">🗑</button>'
            if delete_cli
            else ""
        )
        owner_show = (
            (html.escape(owner) if len(owner) <= 40 else html.escape(owner[:37] + "…"))
            if owner
            else "—"
        )
        owner_title = html.escape(owner) if owner else ""
        cost_raw = r.get("billed_cost_last_30_days")
        cost_data_attr = ""
        if cost_raw is not None:
            try:
                cn = float(cost_raw)
                cost_data_attr = f' data-cost30="{cn:.10f}"'
            except (TypeError, ValueError):
                pass
        tbody_html += (
            f'            <tr data-region="{html.escape(region_norm)}" data-kind="{html.escape(kind_norm)}" data-state="{html.escape(st)}" data-owner="{html.escape(owner_norm)}" data-age="{html.escape(age_bucket)}" data-size-gb="{sz:g}"{cost_data_attr}{delete_cli_attr}>\n'
            f'                <td class="ocid-cell" data-value="{html.escape(ocid)}" title="{html.escape(ocid)}">{link}<code class="ocid-code">{html.escape(ocid_show)}</code>{copy_btn}{delete_btn}</td>\n'
            f'                <td data-value="{html.escape(region)}">{html.escape(region)}</td>\n'
            f'                <td class="comp-cell" data-value="{html.escape(comp)}" title="{html.escape(comp)}">{html.escape(comp)}</td>\n'
            f'                <td data-value="{html.escape(kind)}">{html.escape(kind)}</td>\n'
            f'                <td data-value="{html.escape(disp)}" title="{html.escape(disp)}">{html.escape(disp)}</td>\n'
            f'                <td data-value="{html.escape(st)}">{html.escape(st)}</td>\n'
            f'                <td class="num" data-value="{html.escape(sz_s)}">{html.escape(sz_s)}</td>\n'
            f'                <td class="num" data-value="{html.escape(cost_dv)}">{html.escape(cost_show)}</td>\n'
            f'                <td data-value="{html.escape(owner)}" title="{owner_title}">{owner_show}</td>\n'
            f'                <td data-value="{html.escape(created_disp)}">{html.escape(created_disp) if created_disp else "—"}</td>\n'
            f'                <td class="num" data-value="{html.escape(age_sort)}">{html.escape(age_s) if age_s else "—"}</td>\n'
            f"            </tr>\n"
        )

    filter_row_parts: list[str] = []
    for i, label in enumerate(storage_col_labels):
        unique_vals = sorted(
            col_unique[i],
            key=lambda v: (v == "", v.lower() if isinstance(v, str) else str(v)),
        )
        opts = "".join(
            f'<option value="{html.escape(v) if v else empty_sentinel}">{html.escape(v) if v else "(empty)"}</option>'
            for v in unique_vals
        )
        filter_row_parts.append(
            f'                <th><select data-col="{i}" aria-label="Filter {html.escape(label)}">'
            f'<option value="__all__">(All)</option>{opts}</select></th>'
        )
    filter_row_html = "\n".join(filter_row_parts)

    chart_init_lines: list[str] = []
    if chart_labels:
        chart_init_lines.append(
            "ownerChart = createChart('ownerChart', "
            + json.dumps(chart_labels)
            + ", "
            + json.dumps(chart_values)
            + ", "
            + json.dumps(chart_bg)
            + ", chartFilterOwner, 'owner');"
        )
    if region_labels:
        chart_init_lines.append(
            "regionChart = createChart('regionChart', "
            + json.dumps(region_labels)
            + ", "
            + json.dumps(region_values)
            + ", "
            + json.dumps(region_bg)
            + ", chartFilterRegion, 'region');"
        )
    if age_labels:
        chart_init_lines.append(
            "ageChart = createChart('ageChart', "
            + json.dumps(age_labels)
            + ", "
            + json.dumps(age_values)
            + ", "
            + json.dumps(age_bg)
            + ", chartFilterAge, 'age');"
        )
    chart_init_js = "\n            ".join(chart_init_lines)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unattached volumes</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
        body {{ margin: 0; padding: 0; background: #f8fafc; color: #1e293b; }}
        h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; color: #0f172a; }}
        .subtitle {{ color: #64748b; margin-bottom: 1rem; }}
        .summary {{ font-size: 1.1rem; font-weight: 600; color: #0c4a6e; margin-bottom: 1.5rem; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 0.85rem; }}
        th {{ background: #0f172a; color: white; padding: 0.75rem 0.6rem; text-align: left; font-weight: 500; }}
        td {{ padding: 0.5rem 0.6rem; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
        tr:hover {{ background: #f8fafc; }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        th.sortable {{ cursor: pointer; user-select: none; }}
        th.sortable:hover {{ background: #1e293b; }}
        th.sorted-asc::after {{ content: ' \\25B2'; font-size: 0.7em; opacity: 0.8; }}
        th.sorted-desc::after {{ content: ' \\25BC'; font-size: 0.7em; opacity: 0.8; }}
        .filter-row select {{ width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font-size: 0.875rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .comp-cell {{ max-width: 28ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .ocid-cell {{ max-width: 18ch; white-space: nowrap; }}
        .ocid-code {{ font-size: 0.75rem; white-space: nowrap; }}
        .oci-console-link {{ color: #2563eb; text-decoration: none; margin-right: 0.35rem; font-size: 0.85em; opacity: 0.65; vertical-align: middle; }}
        .oci-console-link:hover {{ opacity: 1; text-decoration: underline; }}
        .copy-ocid-btn, .delete-volume-cli-btn {{ cursor: pointer; margin-left: 0.25rem; opacity: 0.5; font-size: 0.75em; padding: 0 0.2rem; border: none; background: none; vertical-align: middle; }}
        .copy-ocid-btn:hover, .delete-volume-cli-btn:hover {{ opacity: 1; }}
        .delete-volume-cli-btn {{ font-size: 0.85em; }}
        .table-actions {{ margin-bottom: 0.75rem; }}
        .bulk-delete-cli-btn {{ cursor: pointer; font-size: 0.875rem; padding: 0.45rem 0.85rem; border: 1px solid #cbd5e1; border-radius: 6px; background: white; color: #0f172a; }}
        .bulk-delete-cli-btn:hover {{ background: #f8fafc; border-color: #94a3b8; }}
        .bulk-delete-cli-btn:disabled {{ cursor: not-allowed; opacity: 0.55; }}
        .report-content {{ padding: 2rem; max-width: 1600px; margin: 0 auto; }}
        .charts-row {{ display: flex; flex-wrap: wrap; gap: 2rem; margin-bottom: 2rem; align-items: flex-start; }}
        .chart-container {{ flex: 1; min-width: 240px; max-width: 320px; }}
        .chart-title-row {{ display: flex; align-items: center; justify-content: space-between; margin: 0 0 1rem; font-size: 1rem; }}
        .chart-wrapper {{ background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); box-sizing: border-box; }}
        .chart-filter {{ margin-top: 0.75rem; }}
        .chart-filter select {{ width: 100%; padding: 0.35rem 0.5rem; font-size: 0.8rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .reset-filter {{ cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0.2rem; line-height: 1; user-select: none; border-radius: 4px; margin-left: 0.25rem; }}
        .reset-filter:hover {{ opacity: 1; background: rgba(0,0,0,0.06); }}
        .reset-all-filters {{ cursor: pointer; display: inline-block; padding: 0.2rem; opacity: 0.6; font-size: 1.1rem; }}
        .reset-all-filters:hover {{ opacity: 1; }}
        .treemap-wrapper {{ background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 1rem; }}
        .treemap-container {{ position: relative; width: 100%; height: 380px; background: #f8fafc; border-radius: 6px; }}
        .treemap-cell {{ position: absolute; border: 1px solid rgba(15,23,42,0.12); border-radius: 4px; padding: 6px; overflow: hidden; display: flex; flex-direction: column; justify-content: center; box-sizing: border-box; }}
        .treemap-cell:hover {{ border-color: #0f172a; z-index: 1; }}
        .treemap-cell-label {{ font-size: 0.8rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #0f172a; }}
        .treemap-cell-value {{ font-size: 0.7rem; color: #64748b; margin-top: 2px; }}
        .treemap-toggle {{ cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0.2rem; line-height: 1; user-select: none; border-radius: 4px; }}
        .treemap-toggle:hover {{ opacity: 1; background: rgba(0,0,0,0.06); }}
        .treemap-toggle.active {{ opacity: 1; color: #0f172a; }}
        .treemap-section {{ display: none; }}
        .treemap-section.visible {{ display: block; margin-top: 2rem; margin-left: -2rem; margin-right: -2rem; width: calc(100% + 4rem); max-width: 100vw; box-sizing: border-box; }}
        .treemap-section.visible .treemap-wrapper {{ padding-left: 1rem; padding-right: 1rem; box-sizing: border-box; }}
        #treemapsBelowCharts {{ width: 100%; }}
        @media (max-width: 768px) {{ .treemap-container {{ height: 280px; }} }}
        {get_top_nav_css()}
        {get_filter_help_modal_css()}
    </style>
</head>
<body>
{top_nav}
    <div class="report-content">
        <h1>Unattached volumes</h1>
        <p class="subtitle">Block and boot volumes with no active attachment · Owner and created date from defined tags (config.json) · Generated {html.escape(generated_label)}{html.escape(cost_subtitle)}</p>
        <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.5rem;">
            <p class="summary" id="totalGbSummary">{len(rows)} volume(s) · {total_gb:,.1f} GB · {total_cost_all:,.2f} Cost (30d) total (filtered of {len(rows)} · {total_gb:,.1f} GB · {total_cost_all:,.2f} Cost (30d))</p>
            <div style="display: flex; align-items: center; gap: 0.15rem; flex-shrink: 0;">
                <span class="reset-all-filters" title="Reset all filters">&#8635;</span>
                {get_filter_help_nav_icon_html()}
            </div>
        </div>
{charts_row_html}
{treemap_sections_html}
        <div class="table-actions">
            <button type="button" id="copyFilteredDeleteCommandsBtn" class="bulk-delete-cli-btn">Create delete command for all shown (filtered) volumes</button>
        </div>
        <table id="volTable">
            <thead>
                <tr>
                    <th class="sortable" data-col="0">OCID</th>
                    <th class="sortable" data-col="1">Region</th>
                    <th class="sortable" data-col="2">Compartment</th>
                    <th class="sortable" data-col="3">Kind</th>
                    <th class="sortable" data-col="4">Display name</th>
                    <th class="sortable" data-col="5">State</th>
                    <th class="sortable num" data-col="6">Size (GB)</th>
                    <th class="sortable num" data-col="7">Cost (30d)</th>
                    <th class="sortable" data-col="8">Owner</th>
                    <th class="sortable" data-col="9">Created</th>
                    <th class="sortable num" data-col="10">Age (days)</th>
                </tr>
                <tr class="filter-row">
{filter_row_html}
                </tr>
            </thead>
            <tbody>
{tbody_html if tbody_html else '            <tr><td colspan="11">No unattached volumes found.</td></tr>\n'}
            </tbody>
        </table>
    </div>
    <script>
        (function() {{
            const rowsFilterData = {json.dumps(rows_filter_data)};
            const totalRows = {len(rows)};
            const totalGbAll = {total_gb:.10f};
            const totalCostAll = {total_cost_all:.10f};
            const paletteColors = {json.dumps(chart_colors)};
            const ageBucketToColor = {json.dumps(_AGE_BUCKET_TO_COLOR)};
            const totalGbEl = document.getElementById('totalGbSummary');
            const bulkDeleteBtn = document.getElementById('copyFilteredDeleteCommandsBtn');
            const table = document.getElementById('volTable');
            if (!table) return;
            const tbody = table.querySelector('tbody');
            const headers = table.querySelectorAll('thead tr:first-child th.sortable');
            const filters = Array.from(table.querySelectorAll('.filter-row select'));
            const chartFilterOwner = document.getElementById('filterOwner');
            const chartFilterRegion = document.getElementById('filterRegion');
            const chartFilterAge = document.getElementById('filterAge');
            let sortCol = -1, sortAsc = false;
            const numSortCols = new Set([6, 7, 10]);
            const excluded = {{ owner: new Set(), region: new Set(), age: new Set() }};

            function getCellValue(row, col) {{
                const cell = row.cells[col];
                if (!cell) return '';
                if (cell.hasAttribute('data-value')) return cell.getAttribute('data-value') ?? '';
                return cell.textContent.trim();
            }}

            function visibleDeleteCommands() {{
                const lines = [];
                tbody.querySelectorAll('tr').forEach((row) => {{
                    if (row.style.display === 'none' || row.cells.length < 11) return;
                    const cmd = row.getAttribute('data-delete-cli');
                    if (cmd) lines.push(cmd);
                }});
                return lines;
            }}

            function updateBulkDeleteButton() {{
                if (!bulkDeleteBtn) return;
                const n = visibleDeleteCommands().length;
                bulkDeleteBtn.disabled = n === 0;
                bulkDeleteBtn.title = n
                    ? 'Copy ' + n + ' OCI CLI delete command(s) for currently visible volumes'
                    : 'No visible volumes to copy delete commands for';
            }}

            function updateSummaryLine() {{
                if (!totalGbEl) return;
                const vt = visibleTableTotals();
                totalGbEl.textContent = vt.n + ' volume(s) · ' + vt.sumGb.toFixed(1) + ' GB · ' + vt.sumCost.toFixed(2) + ' Cost (30d) total (filtered of ' + totalRows + ' · ' + totalGbAll.toFixed(1) + ' GB · ' + totalCostAll.toFixed(2) + ' Cost (30d))';
                updateBulkDeleteButton();
            }}

            function aggregateFiltered(filtered) {{
                const byOwner = {{}}, byRegion = {{}}, byAge = {{}};
                let totalGb = 0;
                filtered.forEach((r) => {{
                    const g = r.sizeGb || 0;
                    totalGb += g;
                    byOwner[r.owner] = (byOwner[r.owner] || 0) + g;
                    byRegion[r.region] = (byRegion[r.region] || 0) + g;
                    byAge[r.age] = (byAge[r.age] || 0) + g;
                }});
                const ageOrder = ['<7 days','<31 days','<6 months','<1 year','1 to 2 years','2+ years','(Unknown age)'];
                return {{
                    owner: Object.entries(byOwner).sort((a,b) => b[1]-a[1]),
                    region: Object.entries(byRegion).sort((a,b) => b[1]-a[1]),
                    age: Object.entries(byAge).sort((a,b) => (ageOrder.indexOf(a[0])-ageOrder.indexOf(b[0])) || (b[1]-a[1])),
                    totalGb
                }};
            }}

            function visibleTableTotals() {{
                let n = 0, sumGb = 0, sumCost = 0;
                tbody.querySelectorAll('tr').forEach((row) => {{
                    if (row.style.display === 'none' || row.cells.length < 11) return;
                    n++;
                    const sg = row.getAttribute('data-size-gb');
                    const g = parseFloat(sg);
                    if (!isNaN(g)) sumGb += g;
                    const sc = row.getAttribute('data-cost30');
                    if (sc !== null && sc !== '') {{
                        const c = parseFloat(sc);
                        if (!isNaN(c)) sumCost += c;
                    }}
                }});
                return {{ n, sumGb, sumCost }};
            }}

            function updateChart(chart, data, mode) {{
                if (!chart) return;
                chart.data.labels = data.map(([l]) => l);
                chart.data.datasets[0].data = data.map(([,c]) => c);
                if (mode === 'palette') {{
                    chart.data.datasets[0].backgroundColor = data.map((_, i) => paletteColors[i % paletteColors.length]);
                }} else {{
                    chart.data.datasets[0].backgroundColor = data.map(([l]) => ageBucketToColor[l] || '#94a3b8');
                }}
                chart.update();
            }}

            function createChart(canvasId, labels, values, bgColors, filterEl, excludeKey) {{
                const canvas = document.getElementById(canvasId);
                if (!canvas || typeof Chart === 'undefined') return null;
                const ctx = canvas.getContext('2d');
                const opts = {{
                    responsive: true,
                    maintainAspectRatio: true,
                    plugins: {{
                        legend: {{ display: false }},
                        tooltip: {{
                            callbacks: {{
                                label: (ctx) => {{
                                    const n = typeof ctx.parsed === 'number' ? ctx.parsed : 0;
                                    return ctx.label + ': ' + n.toFixed(2) + ' GB';
                                }}
                            }}
                        }}
                    }}
                }};
                if (filterEl && excludeKey) {{
                    opts.onClick = (evt, elements, chart) => {{
                        if (!elements.length) return;
                        const label = chart.data.labels[elements[0].index];
                        const isExclude = (evt && evt.shiftKey) || (evt && evt.native && evt.native.shiftKey);
                        if (isExclude && excludeKey) {{
                            const s = excluded[excludeKey];
                            if (s.has(label)) s.delete(label); else s.add(label);
                            applyAllFilters();
                        }} else if (filterEl) {{
                            filterEl.value = label;
                            applyAllFilters();
                        }}
                    }};
                }}
                const chart = new Chart(ctx, {{
                    type: 'doughnut',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            data: values,
                            backgroundColor: bgColors,
                            borderWidth: 0
                        }}]
                    }},
                    options: opts
                }});
                if (excludeKey) {{
                    canvas.addEventListener('click', (e) => {{
                        if (e.shiftKey) {{
                            const ch = Chart.getChart(canvas);
                            const el = ch?.getElementsAtEventForMode(e, 'nearest', {{ intersect: true }}, false);
                            if (el && el.length && el[0].index !== undefined) {{
                                const label = ch.data.labels[el[0].index];
                                const s = excluded[excludeKey];
                                if (s.has(label)) s.delete(label); else s.add(label);
                                applyAllFilters();
                                e.stopPropagation();
                                e.preventDefault();
                            }}
                        }}
                    }}, true);
                }}
                return chart;
            }}

            let ownerChart = null, regionChart = null, ageChart = null;
            {chart_init_js}

            const chartFilterLabels = {{
                owner: {json.dumps(raw_owner_labels)},
                region: {json.dumps(raw_region_labels)},
                age: {json.dumps(raw_age_labels)}
            }};
            const chartFilterEls = {{ owner: chartFilterOwner, region: chartFilterRegion, age: chartFilterAge }};

            function squarify(data, x, y, w, h) {{
                const values = data.map(([, v]) => Math.max(0, v));
                const total = values.reduce((a, b) => a + b, 0);
                if (total <= 0 || data.length === 0) return [];
                const area = w * h;
                const norm = values.map((v) => (v / total) * area);
                const result = [];
                let idx = 0;
                const sumArr = (a) => a.reduce((s, v) => s + v, 0);
                const worstRatio = (row, length) => {{
                    const s = sumArr(row), mn = Math.min(...row), mx = Math.max(...row);
                    return Math.max(Math.pow(length, 2) * mx / Math.pow(s, 2), Math.pow(s, 2) / (Math.pow(length, 2) * mn));
                }};
                while (idx < norm.length) {{
                    let row = [], worst = Infinity;
                    let j = idx;
                    while (j < norm.length) {{
                        const next = row.concat([norm[j]]);
                        const r = worstRatio(next, next.length);
                        if (row.length === 0 || r <= worst) {{ row = next; worst = r; j++; }}
                        else break;
                    }}
                    const rowSum = sumArr(row);
                    const horizontal = w >= h;
                    const rowSize = horizontal ? rowSum / h : rowSum / w;
                    let cx = x, cy = y;
                    for (let k = 0; k < row.length; k++) {{
                        const a = row[k];
                        const rw = horizontal ? rowSize : a / rowSize;
                        const rh = horizontal ? a / rowSize : rowSize;
                        const [label, cost] = data[idx + k];
                        result.push({{ label, cost, x: cx, y: cy, w: rw, h: rh }});
                        if (horizontal) cy += rh; else cx += rw;
                    }}
                    if (horizontal) {{ x += rowSize; w -= rowSize; }} else {{ y += rowSize; h -= rowSize; }}
                    idx = j;
                }}
                return result;
            }}

            function updateTreemapGb(containerId, data, total, colorArr) {{
                const el = document.getElementById(containerId);
                if (!el) return;
                if (!data.length) {{ el.innerHTML = ''; return; }}
                data = data.filter(([, cost]) => total > 0 && (cost / total) >= 0.01);
                if (!data.length) {{ el.innerHTML = ''; return; }}
                const pad = 4;
                const W = Math.max(100, (el.offsetWidth || el.clientWidth || 600) - pad * 2);
                const H = Math.max(100, (el.offsetHeight || el.clientHeight || 372) - pad * 2);
                const rects = squarify(data, 0, 0, W, H);
                el.innerHTML = rects.map((r, i) => {{
                    const {{ label, cost }} = r;
                    const pct = total ? (cost / total * 100).toFixed(1) : 0;
                    const esc = (s) => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                    const color = Array.isArray(colorArr)
                        ? colorArr[i % colorArr.length]
                        : (colorArr[label] || colorArr['(Unknown age)'] || '#94a3b8');
                    return '<div class="treemap-cell" style="left:' + (pad + r.x) + 'px;top:' + (pad + r.y) + 'px;width:' + Math.max(0, r.w - 1) + 'px;height:' + Math.max(0, r.h - 1) + 'px;background:' + color + ';" title="' + esc(label) + ': ' + cost.toFixed(2) + ' GB (' + pct + '%)"><span class="treemap-cell-label">' + esc(label) + '</span><span class="treemap-cell-value">' + cost.toFixed(2) + ' GB (' + pct + '%)</span></div>';
                }}).join('');
            }}

            function strikethrough(s) {{ return [...String(s)].map(c => c + '\\u0336').join(''); }}
            function updateDropdownStrikethrough() {{
                for (const [key, sel] of Object.entries(chartFilterEls)) {{
                    if (!sel) continue;
                    const labels = chartFilterLabels[key] || [];
                    const excl = excluded[key];
                    const curVal = sel.value;
                    sel.innerHTML = '<option value="">(All)</option>' + labels.map((l) => {{
                        const esc = (x) => String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                        const text = excl.has(l) ? strikethrough(l) : l;
                        return '<option value="' + esc(l) + '">' + esc(text) + '</option>';
                    }}).join('');
                    sel.value = curVal;
                }}
            }}

            const EMPTY_SENTINEL = '__empty__';
            function applyAllFilters() {{
                updateDropdownStrikethrough();
                const ownerVal = chartFilterOwner?.value || '';
                const regionVal = chartFilterRegion?.value || '';
                const ageVal = chartFilterAge?.value || '';
                const filtered = rowsFilterData.filter((r) => {{
                    if (excluded.owner.has(r.owner)) return false;
                    if (excluded.region.has(r.region)) return false;
                    if (excluded.age.has(r.age)) return false;
                    if (ownerVal && r.owner !== ownerVal) return false;
                    if (regionVal && r.region !== regionVal) return false;
                    if (ageVal && r.age !== ageVal) return false;
                    return true;
                }});
                const agg = aggregateFiltered(filtered);
                updateChart(ownerChart, agg.owner, 'palette');
                updateChart(regionChart, agg.region, 'palette');
                updateChart(ageChart, agg.age, 'age');
                updateTreemapGb('treemapStorageOwner', agg.owner, agg.totalGb, paletteColors);
                updateTreemapGb('treemapStorageRegion', agg.region, agg.totalGb, paletteColors);
                updateTreemapGb('treemapStorageAge', agg.age, agg.totalGb, ageBucketToColor);

                const filterValsByCol = {{}};
                filters.forEach((f) => {{
                    const col = parseInt(f.getAttribute('data-col'), 10);
                    if (!isNaN(col)) filterValsByCol[col] = f.value;
                }});
                const numTableCols = 11;
                tbody.querySelectorAll('tr').forEach((row) => {{
                    if (row.cells.length < 11) {{ row.style.display = ''; return; }}
                    const ow = row.getAttribute('data-owner') || '';
                    const rg = row.getAttribute('data-region') || '';
                    const ag = row.getAttribute('data-age') || '';
                    let chartMatch = true;
                    if (excluded.owner.has(ow)) chartMatch = false;
                    if (excluded.region.has(rg)) chartMatch = false;
                    if (excluded.age.has(ag)) chartMatch = false;
                    if (ownerVal && ow !== ownerVal) chartMatch = false;
                    if (regionVal && rg !== regionVal) chartMatch = false;
                    if (ageVal && ag !== ageVal) chartMatch = false;
                    let colMatch = true;
                    for (let j = 0; j < numTableCols; j++) {{
                        const fv = filterValsByCol[j];
                        if (fv && fv !== '__all__') {{
                            let cellVal = getCellValue(row, j);
                            cellVal = (cellVal === '' || cellVal == null) ? EMPTY_SENTINEL : cellVal;
                            const filterVal = fv === EMPTY_SENTINEL ? EMPTY_SENTINEL : fv;
                            if (cellVal !== filterVal) {{ colMatch = false; break; }}
                        }}
                    }}
                    row.style.display = (chartMatch && colMatch) ? '' : 'none';
                }});
                updateSummaryLine();
            }}

            function sort(col) {{
                sortAsc = (sortCol === col) ? !sortAsc : (numSortCols.has(col) ? false : true);
                sortCol = col;
                headers.forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                headers[col]?.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
                const rows = Array.from(tbody.querySelectorAll('tr')).filter((r) => r.style.display !== 'none' && r.cells.length === 11);
                rows.sort((a, b) => {{
                    const va = getCellValue(a, col), vb = getCellValue(b, col);
                    let cmp;
                    if (numSortCols.has(col)) {{
                        const na = parseFloat(va), nb = parseFloat(vb);
                        const vaEmpty = va === '' || va === '__empty__';
                        const vbEmpty = vb === '' || vb === '__empty__';
                        if (vaEmpty && vbEmpty) cmp = 0;
                        else if (vaEmpty) cmp = 1;
                        else if (vbEmpty) cmp = -1;
                        else if (!isNaN(na) && !isNaN(nb)) cmp = na - nb;
                        else cmp = String(va).localeCompare(String(vb), undefined, {{ numeric: true }});
                    }} else {{
                        const na = parseFloat(va), nb = parseFloat(vb);
                        if (!isNaN(na) && !isNaN(nb)) cmp = na - nb;
                        else cmp = String(va).localeCompare(String(vb), undefined, {{ numeric: true }});
                    }}
                    return sortAsc ? cmp : -cmp;
                }});
                rows.forEach((r) => tbody.appendChild(r));
                updateSummaryLine();
            }}

            headers.forEach((h, i) => h.addEventListener('click', () => sort(i)));
            filters.forEach((f) => f.addEventListener('change', () => applyAllFilters()));
            [chartFilterOwner, chartFilterRegion, chartFilterAge].forEach((el) => {{ if (el) el.addEventListener('change', applyAllFilters); }});
            document.querySelectorAll('.treemap-toggle').forEach((btn) => {{
                btn.addEventListener('click', () => {{
                    const sectionId = btn.getAttribute('data-treemap');
                    const section = document.getElementById(sectionId);
                    if (!section) return;
                    const wasVisible = section.classList.contains('visible');
                    document.querySelectorAll('.treemap-section').forEach((s) => s.classList.remove('visible'));
                    document.querySelectorAll('.treemap-toggle').forEach((b) => b.classList.remove('active'));
                    if (!wasVisible) {{
                        section.classList.add('visible');
                        btn.classList.add('active');
                        section.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
                        requestAnimationFrame(() => {{ requestAnimationFrame(() => applyAllFilters()); }});
                    }}
                }});
            }});
            document.querySelectorAll('.reset-filter').forEach((btn) => {{
                btn.addEventListener('click', () => {{
                    const key = btn.getAttribute('data-reset');
                    if (key && excluded[key]) excluded[key].clear();
                    const el = chartFilterEls[key];
                    if (el) el.value = '';
                    updateDropdownStrikethrough();
                    applyAllFilters();
                }});
            }});
            document.querySelectorAll('.reset-all-filters').forEach((btn) => {{
                btn.addEventListener('click', () => {{
                    excluded.owner.clear(); excluded.region.clear(); excluded.age.clear();
                    [chartFilterOwner, chartFilterRegion, chartFilterAge].forEach((el) => {{ if (el) el.value = ''; }});
                    filters.forEach((f) => {{ f.value = '__all__'; }});
                    updateDropdownStrikethrough();
                    applyAllFilters();
                }});
            }});
            tbody.addEventListener('click', (e) => {{
                const btn = e.target.closest('.copy-ocid-btn, .delete-volume-cli-btn');
                if (btn && btn.getAttribute('data-copy')) {{
                    navigator.clipboard.writeText(btn.getAttribute('data-copy')).then(() => {{
                        const orig = btn.textContent;
                        btn.textContent = '✓';
                        setTimeout(() => {{ btn.textContent = orig; }}, 800);
                    }});
                }}
            }});
            if (bulkDeleteBtn) {{
                bulkDeleteBtn.addEventListener('click', () => {{
                    const lines = visibleDeleteCommands();
                    if (!lines.length) return;
                    navigator.clipboard.writeText(lines.join('\\n') + '\\n').then(() => {{
                        const orig = bulkDeleteBtn.textContent;
                        bulkDeleteBtn.textContent = 'Copied ' + lines.length + ' delete command(s) ✓';
                        setTimeout(() => {{ bulkDeleteBtn.textContent = orig; }}, 2000);
                    }});
                }});
            }}
            applyAllFilters();
            sort(7);
        }})();
    </script>
{get_filter_help_modal_html()}
    <script>
{get_filter_help_modal_init_js()}
    </script>
</body>
</html>
"""
    output_path.write_text(html_content, encoding="utf-8")


def load_storage_json(path: Path) -> tuple[list[dict], str, str]:
    """Load rows, display label, and optional subtitle suffix (cost window) from getStorageReport output."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    label = str(data.get("generated_label") or data.get("generated_at") or "").strip()
    if not label:
        label = "unknown"
    cost_subtitle = ""
    end = data.get("cost_basis_end_date")
    days = data.get("cost_window_days")
    if end and days:
        cost_subtitle = f" · Billed cost (30d) through {end} from consolidated FOCUS CSV"
    return rows, label, cost_subtitle


def load_unattached_volume_ocid_set(json_path: Path) -> frozenset[str]:
    """
    OCIDs of unattached boot or block volumes from getStorageReport JSON.
    Used by per-user dashboards to flag cost rows whose ResourceId matches.
    Returns an empty set if the file is missing or invalid.
    """
    if not json_path.is_file():
        return frozenset()
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    out: set[str] = set()
    for row in data.get("rows") or []:
        ocid = (row.get("ocid") or "").strip()
        kind = (row.get("volume_kind") or "").strip().lower()
        if ocid and kind in ("boot", "block"):
            out.add(ocid)
    return frozenset(out)


##########################################################################
# Main Program
##########################################################################


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    default_in = script_dir / "consolidateReports" / "StorageReport" / STORAGE_JSON_DEFAULT
    default_out = script_dir / "FocusReports" / "ConsolidatedHTML" / "storage.html"

    parser = argparse.ArgumentParser(
        description="Build storage.html from JSON produced by getStorageReport.py (no OCI calls)."
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=default_in,
        help=f"Path to unattached volumes JSON (default: {default_in})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_out,
        help=f"Output HTML path (default: {default_out})",
    )
    args = parser.parse_args()

    in_path: Path = args.input
    if not in_path.is_file():
        print(f"Input file not found: {in_path}", file=sys.stderr)
        print("Run getStorageReport.py first to fetch data from OCI.", file=sys.stderr)
        sys.exit(1)

    rows, generated_label, cost_subtitle = load_storage_json(in_path)
    out_path: Path = args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    consolidated_dir = out_path.parent

    print("OCI - OCVS Focus Reports — make storage dashboard (offline)")
    print("============================================================================================")
    print(f"Input:  {in_path}")
    print(f"Rows:   {len(rows)}")
    print(f"Output: {out_path}")

    generate_storage_html(rows, out_path, generated_label, consolidated_dir, cost_subtitle)
    print(f"\nWrote dashboard: {out_path}")


if __name__ == "__main__":
    main()
