"""
Consolidate FOCUS reports into daily reports by ResourceID + SkuId.
- Groups by (ResourceId, SkuId) since the same resource can have multiple charge types
  (e.g. Block Volume Storage vs Performance Units)
- Sums UsageQuantity, PricingQuantity, BilledCost, EffectiveCost, and related fields
- For varying fields, uses the record with the latest ChargePeriodEnd
- Output CSV: FocusReports/ConsolidatedCSV/YYYY/MM/YYYY-MM-DD.csv
- Output HTML: FocusReports/ConsolidatedHTML/YYYY/MM/YYYY-MM-DD.html
- Default: skip a day only if consolidated CSV+HTML exist and are newer than or equal to all focus inputs for that day (new/changed inputs always regenerate; use -force to rebuild everything). Stale checks apply only to the last N calendar days (inclusive of today); see dailyReportStaleCheckDays in config.json.
"""

from __future__ import annotations
import argparse
import calendar
import csv
import html
import json
import re
from pathlib import Path

from ocimodules.settings import load_settings
from urllib.parse import quote
from datetime import date, datetime, timedelta
from collections import defaultdict

from reportNav import (
    base_path_from_output,
    find_latest_monthly_report,
    get_consolidated_dir_from_output,
    get_filter_help_modal_css,
    get_filter_help_modal_html,
    get_filter_help_modal_init_js,
    get_filter_help_nav_icon_html,
    get_top_nav_css,
    get_top_nav_html,
)
from consolidateReports import (
    attach_master_console_links,
    build_rows_filter_item,
    combined_items_info_btn_html,
    deleted_resource_icon_html,
    enrich_rows_with_master_resource,
    enrich_rows_with_resource_names,
    get_combined_items_js_helpers,
    get_combined_items_modal_css,
    get_combined_items_modal_html,
    get_deleted_resource_icon_css,
    master_oci_console_link_html,
    masters_with_multiple_resources,
)

# Fields to sum when consolidating (numeric, additive)
SUM_FIELDS = {
    "UsageQuantity",
    "PricingQuantity",
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "oci_UnitPriceOverage",
    "oci_BilledQuantityOverage",
    "oci_CostOverage",
    "oci_AttributedUsage",
    "oci_AttributedCost",
}

# Timestamp field used to determine "latest" record for varying fields
DATESTAMP_FIELD = "ChargePeriodEnd"

# Default tag keys for CreatedBy/CreatedDate (overridden by config.json)
DEFAULT_TAG_CREATED_BY = "Owner.CreatedBy"
DEFAULT_TAG_CREATED_DATE = "Owner.CreatedDate"


def load_config() -> dict:
    """Settings from config.json merged over built-in defaults (see ocimodules/settings.py)."""
    return load_settings()


def get_daily_report_stale_check_days() -> int | None:
    """
    Days (inclusive of today) for which focus vs consolidated mtime is evaluated.
    Returns None if config disables the limit (0 or negative) — stale checks apply to all days.
    Default when missing or invalid: 5.
    """
    config = load_config()
    raw = config.get("dailyReportStaleCheckDays", 5)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 5
    if n <= 0:
        return None
    return n


def stale_check_cutoff_date() -> date | None:
    """First calendar date (inclusive) in the stale-check window, or None for no window."""
    n = get_daily_report_stale_check_days()
    if n is None:
        return None
    return date.today() - timedelta(days=n - 1)


def get_tag_config() -> tuple[str, str]:
    """Return (createdBy_tag, createdDate_tag) from config or defaults."""
    config = load_config()
    tags = config.get("tags") or {}
    created_by = tags.get("createdBy") or DEFAULT_TAG_CREATED_BY
    created_date = tags.get("createdDate") or DEFAULT_TAG_CREATED_DATE
    return created_by, created_date


def _normalize_schedule_for_deletion_tag_value(raw) -> str:
    """
    OCI list/enum defined tags for ScheduleForDeletion use exact strings "True" and "False"
    (not JSON booleans). Normalize config and Python bools to those strings.
    """
    if raw is None:
        return "True"
    if isinstance(raw, bool):
        return "True" if raw else "False"
    s = str(raw).strip()
    if not s:
        return "True"
    low = s.lower()
    if low == "true":
        return "True"
    if low == "false":
        return "False"
    return s


def get_unattached_volume_deletion_tag_config() -> tuple[bool, str, str, str, int, str]:
    """
    Settings for tagging unattached block/boot volumes (getStorageReport.py).

    Returns:
        enabled: from SetDeletionForUnattachedDisk
        namespace: tag namespace (e.g. Optimization)
        schedule_key, next_schedule_key: tag keys under that namespace
        days_ahead: NextDeletionSchedule = today + N days (date only, ISO)
        schedule_value: value for ScheduleForDeletion — always "True" or "False" per OCI enum strings
    """
    config = load_config()
    enabled = bool(config.get("SetDeletionForUnattachedDisk"))
    u = config.get("unattachedVolumeDeletionTags") or {}
    if not isinstance(u, dict):
        u = {}
    namespace = str(u.get("namespace") or "Optimization").strip() or "Optimization"
    schedule_key = str(u.get("scheduleForDeletionKey") or "ScheduleForDeletion").strip() or "ScheduleForDeletion"
    next_key = str(u.get("nextDeletionScheduleKey") or "NextDeletionSchedule").strip() or "NextDeletionSchedule"
    try:
        days_ahead = int(u.get("deletionScheduleDaysAhead", 14))
    except (TypeError, ValueError):
        days_ahead = 14
    if days_ahead < 0:
        days_ahead = 14

    raw_sched = u.get("scheduleForDeletionValue", None)
    schedule_value = _normalize_schedule_for_deletion_tag_value(raw_sched)

    return enabled, namespace, schedule_key, next_key, days_ahead, schedule_value


def get_usd_conversion_rate() -> float | None:
    """Return USD conversion rate from config if set and valid, else None.
    Rate multiplies source currency to get USD (e.g. 0.012 for INR -> USD)."""
    config = load_config()
    rate = config.get("usdConversionRate")
    if rate is None:
        return None
    try:
        r = float(rate)
        if r > 0:
            return r
    except (TypeError, ValueError):
        pass
    return None


def to_display_cost(cost: float) -> float:
    """Convert cost to display currency. If usdConversionRate is set, returns cost in USD."""
    rate = get_usd_conversion_rate()
    if rate is not None:
        return cost * rate
    return cost


def find_most_recent_daily_consolidated_csv(consolidated_dir: Path) -> tuple[Path, str] | None:
    """
    Find the most recent daily consolidated CSV under consolidated_dir (YYYY/MM/YYYY-MM-DD.csv).
    Skips monthly files named YYYY-MM.csv. Returns (path, date_str) or None.
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
                    continue
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


def find_daily_reports_last_n_days(
    consolidated_dir: Path,
    end_date_str: str,
    n_days: int = 30,
) -> list[tuple[Path, str]]:
    """
    Daily consolidated CSVs for the last n_days ending at end_date_str (inclusive), oldest first.
    Only includes files that exist under consolidated_dir.
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", end_date_str)
    if not m:
        return []
    end_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    start_date = end_date - timedelta(days=n_days - 1)

    path_by_date: dict[tuple[int, int, int], Path] = {}
    pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.csv$")
    for csv_path in consolidated_dir.rglob("*.csv"):
        rel = csv_path.relative_to(consolidated_dir)
        parts = rel.parts
        if len(parts) >= 3:
            try:
                year, month = int(parts[0]), int(parts[1])
                if csv_path.name == f"{year:04d}-{month:02d}.csv":
                    continue
            except (ValueError, IndexError):
                pass
        match = pattern.match(csv_path.name)
        if match:
            y, mo, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
            dte = date(y, mo, d)
            if start_date <= dte <= end_date:
                path_by_date[(y, mo, d)] = csv_path

    result: list[tuple[Path, str]] = []
    for i in range(n_days):
        dte = start_date + timedelta(days=i)
        key = (dte.year, dte.month, dte.day)
        if key in path_by_date:
            result.append((path_by_date[key], dte.strftime("%Y-%m-%d")))
    return result


def sum_billed_cost_by_resource_ids_last_n_days(
    consolidated_dir: Path,
    end_date_str: str,
    resource_ids: set[str],
    n_days: int = 30,
) -> dict[str, float]:
    """
    Sum BilledCost (after to_display_cost) per ResourceId across existing daily CSVs in the window.
    Multiple rows per day (e.g. different SkuId) are all summed. Missing files are skipped.
    """
    if not resource_ids:
        return {}
    want = {rid.strip() for rid in resource_ids if (rid or "").strip()}
    if not want:
        return {}
    totals: dict[str, float] = {rid: 0.0 for rid in want}
    daily_reports = find_daily_reports_last_n_days(consolidated_dir, end_date_str, n_days)
    for csv_path, _ in daily_reports:
        try:
            with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rid = (row.get("ResourceId") or "").strip()
                    if rid not in want:
                        continue
                    try:
                        cost = to_display_cost(float(row.get("BilledCost") or 0))
                    except (ValueError, TypeError):
                        cost = 0.0
                    totals[rid] += cost
        except OSError:
            continue
    return totals


def parse_tags_owner(tags_str: str, created_by_key: str | None = None, created_date_key: str | None = None) -> tuple[str, str]:
    """Extract CreatedBy and CreatedDate from Tags JSON. Returns (created_by, created_date)."""
    created_by = ""
    created_date = ""
    if not tags_str or not str(tags_str).strip():
        return created_by, created_date
    key_by = created_by_key or get_tag_config()[0]
    key_date = created_date_key or get_tag_config()[1]
    try:
        tags = json.loads(tags_str)
        if isinstance(tags, dict):
            created_by = (tags.get(key_by) or "").strip()
            created_date = (tags.get(key_date) or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return created_by, created_date


def age_in_days(created_date_str: str, reference_date: date | None = None) -> str:
    """Return days between reference_date (or today) and CreatedDate. Empty string if date invalid."""
    if not created_date_str or not str(created_date_str).strip():
        return ""
    ref = reference_date or date.today()
    try:
        s = str(created_date_str).strip().rstrip("Z").split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                delta = ref - dt.date()
                return str(delta.days)
            except ValueError:
                continue
    except (ValueError, TypeError):
        pass
    return ""


def format_created_date_display(value: str) -> str:
    """Format OwnerCreatedDate for display: YYYY-MM-DD HH:MM. Returns original if unparseable."""
    if not value or not str(value).strip():
        return ""
    try:
        s = str(value).strip().rstrip("Z").split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
    except (ValueError, TypeError):
        pass
    return str(value).strip()


def parse_datestamp(value: str) -> datetime | None:
    """Parse ChargePeriodEnd (e.g. 2026-03-03T03:00Z) to datetime."""
    if not value or not str(value).strip():
        return None
    try:
        s = str(value).strip().rstrip("Z").split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
    except (ValueError, TypeError):
        return None


def find_focus_report_csvs(base_dir: str) -> dict[tuple[int, int, int], list[tuple[Path, float]]]:
    """
    Find all CSV files under base_dir. Expects structure: .../YYYY/MM/DD/*.csv
    Returns dict mapping (year, month, day) -> list of (path, mtime) for fast skip checks.
    """
    base = Path(base_dir)
    by_date: dict[tuple[int, int, int], list[tuple[Path, float]]] = defaultdict(list)

    for csv_path in base.rglob("*.csv"):
        parts = csv_path.relative_to(base).parts
        if len(parts) >= 3:
            try:
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                try:
                    mtime = csv_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                by_date[(year, month, day)].append((csv_path, mtime))
            except (ValueError, IndexError):
                pass

    return dict(by_date)


def consolidated_outputs_fresher_than_focus_inputs(
    focus_paths: list[Path],
    consolidated_csv: Path,
    consolidated_html: Path,
) -> bool:
    """
    True if both consolidated outputs exist and are not older than any focus CSV for that day.
    Re-stat inputs so new or updated focus files always trigger regeneration.
    """
    if not consolidated_csv.exists() or not consolidated_html.exists():
        return False
    if not focus_paths:
        return False
    try:
        max_in = max(p.stat().st_mtime for p in focus_paths)
    except OSError:
        return False
    try:
        out_mtime = max(consolidated_csv.stat().st_mtime, consolidated_html.stat().st_mtime)
    except OSError:
        return False
    return max_in <= out_mtime


def read_csv_rows(path: Path, date_key: tuple[int, int, int]) -> list[dict]:
    """Read a FOCUS CSV and return list of row dicts with ReportDate added."""
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return rows
        for row in reader:
            row["_ReportDate"] = date_key
            rows.append(row)
    return rows


def consolidate_day(rows: list[dict], fieldnames: list[str]) -> list[dict]:
    """
    Consolidate rows by ResourceId + SkuId (same resource can have multiple SKUs, e.g. Storage + Performance Units):
    - Sum UsageQuantity and other SUM_FIELDS
    - For other fields, use the value from the record with latest ChargePeriodEnd
    """
    # Group by (ResourceId, SkuId) - SkuId distinguishes charge types for the same resource
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        rid = (row.get("ResourceId") or "").strip()
        sku = (row.get("SkuId") or "").strip()
        groups[(rid, sku)].append(row)

    result = []
    for (resource_id, _sku), group_rows in groups.items():
        # Sort by ChargePeriodEnd descending (latest first)
        def sort_key(r):
            ts = parse_datestamp(r.get(DATESTAMP_FIELD) or "")
            return ts or datetime.min

        sorted_rows = sorted(group_rows, key=sort_key, reverse=True)
        latest = sorted_rows[0]

        consolidated = {}
        for col in fieldnames:
            if col in SUM_FIELDS:
                total = 0
                for r in group_rows:
                    v = r.get(col)
                    if v is not None and str(v).strip() != "":
                        try:
                            total += float(v)
                        except (ValueError, TypeError):
                            pass
                consolidated[col] = f"{total:.15f}".rstrip("0").rstrip(".") if total != int(total) else str(int(total))
            else:
                consolidated[col] = latest.get(col, "")

        consolidated["ResourceId"] = resource_id or latest.get("ResourceId", "")
        # Extract Owner tags from Tags JSON
        created_by, created_date = parse_tags_owner(latest.get("Tags") or "")
        consolidated["OwnerCreatedBy"] = created_by
        consolidated["OwnerCreatedDate"] = created_date
        # Age = days between report date and CreatedDate
        report_date = None
        if group_rows and "_ReportDate" in group_rows[0]:
            y, m, d = group_rows[0]["_ReportDate"]
            report_date = date(y, m, d)
        consolidated["OwnerCreatedAgeDays"] = age_in_days(created_date, report_date)
        result.append(consolidated)

    return result


def consolidate_month(daily_rows: list[dict], fieldnames: list[str], year: int, month: int) -> list[dict]:
    """
    Consolidate daily consolidated rows into a monthly report.
    Groups by (ResourceId, SkuId), sums SUM_FIELDS, uses latest for varying fields.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in daily_rows:
        rid = (row.get("ResourceId") or "").strip()
        sku = (row.get("SkuId") or "").strip()
        groups[(rid, sku)].append(row)

    result = []
    for (resource_id, _sku), group_rows in groups.items():
        # Sort by ChargePeriodEnd descending (latest first)
        def sort_key(r):
            ts = parse_datestamp(r.get(DATESTAMP_FIELD) or "")
            return ts or datetime.min

        sorted_rows = sorted(group_rows, key=sort_key, reverse=True)
        latest = sorted_rows[0]

        consolidated = {}
        for col in fieldnames:
            if col in SUM_FIELDS:
                total = 0
                for r in group_rows:
                    v = r.get(col)
                    if v is not None and str(v).strip() != "":
                        try:
                            total += float(v)
                        except (ValueError, TypeError):
                            pass
                consolidated[col] = f"{total:.15f}".rstrip("0").rstrip(".") if total != int(total) else str(int(total))
            else:
                consolidated[col] = latest.get(col, "")

        consolidated["ResourceId"] = resource_id or latest.get("ResourceId", "")
        consolidated["OwnerCreatedBy"] = latest.get("OwnerCreatedBy", "")
        created_date = latest.get("OwnerCreatedDate", "")
        consolidated["OwnerCreatedDate"] = created_date
        report_date = date(year, month, days_in_month)
        consolidated["OwnerCreatedAgeDays"] = age_in_days(created_date, report_date)
        result.append(consolidated)

    return result


def cost_by_service_category(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate BilledCost by ServiceCategory. Uses ServiceName when ServiceCategory is unknown."""
    by_cat: dict[str, float] = defaultdict(float)
    for row in rows:
        cat = (row.get("ServiceCategory") or "").strip()
        if not cat:
            cat = (row.get("ServiceName") or "").strip() or "(Uncategorized)"
        try:
            cost = float(row.get("BilledCost") or 0)
        except (ValueError, TypeError):
            cost = 0
        by_cat[cat] += to_display_cost(cost)
    return sorted(by_cat.items(), key=lambda x: -x[1])


def cost_by_compartment(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate BilledCost by compartment (oci_CompartmentName or oci_CompartmentPath)."""
    by_comp: dict[str, float] = defaultdict(float)
    for row in rows:
        comp = (row.get("oci_CompartmentName") or "").strip()
        if not comp:
            comp = (row.get("oci_CompartmentPath") or "").strip() or "(Uncategorized)"
        try:
            cost = float(row.get("BilledCost") or 0)
        except (ValueError, TypeError):
            cost = 0
        by_comp[comp] += to_display_cost(cost)
    return sorted(by_comp.items(), key=lambda x: -x[1])


ANON_MAPPING_FILENAME = "anon_mapping.json"


def _anon_mapping_path(output_root: Path) -> Path:
    """Path to the persisted username->anon mapping file."""
    return Path(output_root) / ANON_MAPPING_FILENAME


def load_anon_mapping(output_root: Path) -> dict:
    """
    Load persisted anon mapping from FocusReports/anon_mapping.json.
    Returns {"mapping": {...}, "next": int} or {"mapping": {}, "next": 1} if missing/invalid.
    """
    path = _anon_mapping_path(output_root)
    if not path.exists():
        return {"mapping": {}, "next": 1}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        mapping = data.get("mapping") or {}
        next_val = data.get("next")
        if next_val is None:
            # Infer next from max anon number
            max_id = 0
            for v in mapping.values():
                m = re.match(r"^anon(\d+)$", str(v))
                if m:
                    max_id = max(max_id, int(m.group(1)))
            next_val = max_id + 1
        return {"mapping": dict(mapping), "next": int(next_val)}
    except (json.JSONDecodeError, OSError, TypeError):
        return {"mapping": {}, "next": 1}


def save_anon_mapping(output_root: Path, anon_state: dict) -> None:
    """Persist anon mapping to FocusReports/anon_mapping.json."""
    path = _anon_mapping_path(output_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"mapping": anon_state["mapping"], "next": anon_state["next"]},
                f,
                indent=2,
            )
    except OSError as e:
        print(f"Warning: could not save anon mapping to {path}: {e}")


def _is_already_anon(owner: str) -> bool:
    """True if owner already looks like anon##### (e.g. from prior anon run)."""
    return bool(owner and re.match(r"^anon\d+$", str(owner).strip()))


def apply_anon_to_rows(rows: list[dict], anon_state: dict) -> None:
    """
    Replace OwnerCreatedBy in rows with anonymized names (mutates in place).
    anon_state: {"mapping": {owner -> anon#####}, "next": int} - updated as new owners are seen.
    Skips empty owner and strings already matching anon#####.
    """
    mapping = anon_state["mapping"]
    for row in rows:
        owner = (row.get("OwnerCreatedBy") or "").strip()
        if not owner or _is_already_anon(owner):
            continue
        if owner in mapping:
            row["OwnerCreatedBy"] = mapping[owner]
        else:
            next_id = anon_state["next"]
            width = max(5, len(str(next_id + 999)))
            anon_name = f"anon{next_id:0{width}d}"
            mapping[owner] = anon_name
            anon_state["next"] = next_id + 1
            row["OwnerCreatedBy"] = anon_name


def sanitize_owner_filename(owner: str) -> str:
    """Convert owner string to a safe filename (URL-safe, no path traversal). Matches perUserReport."""
    if not owner or not str(owner).strip():
        return "no-owner"
    s = str(owner).strip()
    s = re.sub(r"[^\w\-_.]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:120] if s else "no-owner"


# OCID segment after realm (e.g. iad) -> console region query value (e.g. us-ashburn-1)
_OCI_OCID_SHORT_REGION_TO_ID: dict[str, str] = {
    "iad": "us-ashburn-1",
    "phx": "us-phoenix-1",
    "fra": "eu-frankfurt-1",
    "lhr": "uk-london-1",
    "bom": "ap-mumbai-1",
    "jed": "me-jeddah-1",
    "nrt": "ap-tokyo-1",
    "icn": "ap-seoul-1",
    "kix": "ap-osaka-1",
    "syd": "ap-sydney-1",
    "yyz": "ca-toronto-1",
    "yul": "ca-montreal-1",
    "gru": "sa-saopaulo-1",
    "ams": "eu-amsterdam-1",
    "arn": "eu-stockholm-1",
    "zrh": "eu-zurich-1",
    "dxb": "me-dubai-1",
    "hyd": "ap-hyderabad-1",
    "sin": "ap-singapore-1",
    "mel": "ap-melbourne-1",
    "lin": "eu-milan-1",
    "cdg": "eu-paris-1",
    "mtz": "il-jerusalem-1",
    "sjc": "us-sanjose-1",
    "ord": "us-chicago-1",
}

# (normalized_service, normalized_resource_type) -> url_prefix; "*" = any service (legacy rows)
_resource_type_console_prefixes: dict[tuple[str, str], str] | None = None


def _normalize_resource_type_console_key(s: str) -> str:
    return re.sub(r"[\s_]+", "", (s or "").lower())


def _normalize_csv_fieldnames(fieldnames: list[str] | None) -> list[str] | None:
    if not fieldnames:
        return fieldnames
    return [(fn or "").lstrip("\ufeff").strip() for fn in fieldnames]


def load_resource_type_console_prefixes() -> dict[tuple[str, str], str]:
    """
    Load (service_name, resource_type) -> url_prefix from resourceTypeOciConsole.csv.
    Columns: service_name, resource_type, url_prefix. Matching is case-insensitive; spaces
    and underscores are ignored in both name and type. Rows with empty service_name match
    any ServiceName (wildcard) for that resource_type (legacy two-column CSV).
    """
    global _resource_type_console_prefixes
    if _resource_type_console_prefixes is not None:
        return _resource_type_console_prefixes
    out: dict[tuple[str, str], str] = {}
    path = Path(__file__).resolve().parent / "resourceTypeOciConsole.csv"
    if not path.exists():
        _resource_type_console_prefixes = out
        return out
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            reader.fieldnames = _normalize_csv_fieldnames(reader.fieldnames)
            for row in reader:
                if not row:
                    continue
                svc = (row.get("service_name") or "").strip()
                rt = (row.get("resource_type") or "").strip()
                pref = (row.get("url_prefix") or "").strip()
                if not rt or not pref:
                    continue
                rk = _normalize_resource_type_console_key(rt)
                if not rk:
                    continue
                if svc:
                    sk = _normalize_resource_type_console_key(svc)
                    if sk:
                        out[(sk, rk)] = pref
                else:
                    out[("*", rk)] = pref
    except OSError:
        pass
    _resource_type_console_prefixes = out
    return out


def _region_token_from_ocid(ocid: str) -> str:
    parts = ocid.split(".")
    if len(parts) < 4 or parts[0] != "ocid1":
        return ""
    return parts[3]


def region_for_oci_console(ocid: str, billing_region: str) -> str:
    """
    Region for ?region= in console URLs: prefer parsing the OCID (token after realm),
    else use billing Region if it already looks like an API region id (e.g. us-ashburn-1).
    """
    token = _region_token_from_ocid(ocid)
    if token:
        if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)+-\d+$", token, re.I):
            return token.lower()
        head = token.split("-")[0].lower()
        if head in _OCI_OCID_SHORT_REGION_TO_ID:
            return _OCI_OCID_SHORT_REGION_TO_ID[head]
    br = (billing_region or "").strip()
    if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)+-\d+$", br, re.I):
        return br.lower()
    return ""


def build_oci_console_href(url_prefix: str, ocid: str, region: str) -> str:
    p = (url_prefix or "").strip()
    base = "https://cloud.oracle.com/"
    # Query-style prefix (e.g. loganalytics/home?id=...&comp=): OCID completes a param — no slash before it.
    if "?" in p:
        href = f"{base}{p.rstrip('/')}{ocid}"
    else:
        href = f"{base}{p.strip().strip('/')}/{ocid}"
    if region:
        sep = "&" if "?" in href else "?"
        href = f"{href}{sep}region={quote(region)}"
    return href


def oci_console_link_html(row: dict, prefixes: dict[tuple[str, str], str]) -> str:
    """
    If (service name, ResourceType) matches resourceTypeOciConsole.csv (case-insensitive)
    and we have an OCID (ResourceId or ResourceName starting with ocid1.), return a goto link.
    Service name is taken from ServiceName, or ServiceCategory if ServiceName is empty.
    """
    if not prefixes:
        return ""
    svc_raw = (row.get("ServiceName") or "").strip() or (row.get("ServiceCategory") or "").strip()
    svc_key = _normalize_resource_type_console_key(svc_raw)
    rtype_key = _normalize_resource_type_console_key((row.get("ResourceType") or "").strip())
    url_prefix = prefixes.get((svc_key, rtype_key)) or (
        prefixes.get(("*", rtype_key)) if rtype_key else None
    )
    if not url_prefix:
        return ""
    rid = (row.get("ResourceId") or "").strip()
    rname = (row.get("ResourceName") or "").strip()
    ocid = rid if rid.startswith("ocid1.") else (rname if rname.startswith("ocid1.") else "")
    if not ocid:
        return ""
    region = region_for_oci_console(ocid, (row.get("Region") or "").strip())
    href = build_oci_console_href(url_prefix, ocid, region)
    return (
        f'<a href="{html.escape(href)}" class="oci-console-link" target="_blank" '
        f'rel="noopener noreferrer" title="Open in OCI Console" aria-label="Open in OCI Console">↗</a>'
    )


def cost_by_owner(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate BilledOwnerCreatedBy (from Tags)."""
    by_owner: dict[str, float] = defaultdict(float)
    for row in rows:
        owner = (row.get("OwnerCreatedBy") or "").strip()
        if not owner:
            owner = "(No owner)"
        try:
            cost = float(row.get("BilledCost") or 0)
        except (ValueError, TypeError):
            cost = 0
        by_owner[owner] += to_display_cost(cost)
    return sorted(by_owner.items(), key=lambda x: -x[1])


def _age_bucket(days_str: str) -> str:
    """Map OwnerCreatedAgeDays to bucket: <7 days, <31 days, <6 months, <1 year, 1 to 2 years, 2+ years."""
    if not days_str or not str(days_str).strip():
        return "(Unknown age)"
    try:
        d = int(str(days_str).strip())
        if d < 0:
            return "(Unknown age)"
        if d < 7:
            return "<7 days"
        if d < 31:
            return "<31 days"
        if d < 180:  # ~6 months
            return "<6 months"
        if d < 365:
            return "<1 year"
        if d < 730:  # 2 years
            return "1 to 2 years"
        return "2+ years"
    except (ValueError, TypeError):
        return "(Unknown age)"


def cost_by_age(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate BilledCost by age bucket (OwnerCreatedAgeDays)."""
    order = ["<7 days", "<31 days", "<6 months", "<1 year", "1 to 2 years", "2+ years", "(Unknown age)"]
    by_age: dict[str, float] = defaultdict(float)
    for row in rows:
        bucket = _age_bucket(row.get("OwnerCreatedAgeDays") or "")
        try:
            cost = float(row.get("BilledCost") or 0)
        except (ValueError, TypeError):
            cost = 0
        by_age[bucket] += to_display_cost(cost)
    return sorted(by_age.items(), key=lambda x: (order.index(x[0]) if x[0] in order else 999, -x[1]))


def _row_filter_val(row: dict, field: str) -> str:
    """Extract filter dimension value from a consolidated row (daily or monthly). Matches client rowsFilterData."""
    if field == "serviceCategory":
        v = (row.get("ServiceCategory") or "").strip()
        return v or (row.get("ServiceName") or "").strip() or "(Uncategorized)"
    if field == "compartment":
        v = (row.get("oci_CompartmentName") or "").strip()
        return v or (row.get("oci_CompartmentPath") or "").strip() or "(Uncategorized)"
    if field == "owner":
        v = (row.get("OwnerCreatedBy") or "").strip()
        return v or "(No owner)"
    if field == "region":
        v = (row.get("Region") or "").strip()
        return v or "(Uncategorized)"
    if field == "age":
        return _age_bucket(row.get("OwnerCreatedAgeDays") or "")
    return ""


def cost_by_region(rows: list[dict]) -> list[tuple[str, float]]:
    """Aggregate BilledCost by Region."""
    by_region: dict[str, float] = defaultdict(float)
    for row in rows:
        region = (row.get("Region") or "").strip()
        if not region:
            region = "(Uncategorized)"
        try:
            cost = float(row.get("BilledCost") or 0)
        except (ValueError, TypeError):
            cost = 0
        by_region[region] += to_display_cost(cost)
    return sorted(by_region.items(), key=lambda x: -x[1])


# Stacked daily bar chart colors (JSON-embedded for client; updated when filters change)
DAILY_BAR_CHART_COLORS = [
    "#1a365d", "#2c5282", "#2b6cb0", "#3182ce", "#4299e1",
    "#63b3ed", "#90cdf4", "#bee3f8", "#ebf8ff", "#065f46",
    "#047857", "#059669", "#10b981", "#34d399", "#581c87",
    "#6b21a8", "#7e22ce", "#9333ea", "#a855f7", "#c084fc",
]


def monthly_daily_bar_chart_section_html(heading: str) -> str:
    """HTML shell for stacked bar chart; Chart.js instance is created in main script (filter-aware)."""
    return f"""
    <div class="daily-bar-chart-section">
        <h2 style="margin: 2rem 0 1rem; font-size: 1rem;">{html.escape(heading)}</h2>
        <div class="daily-bar-chart-wrapper">
            <canvas id="monthlyDailyBarChart" height="280"></canvas>
        </div>
    </div>"""


# Table columns to display (subset of schema). ResourceName displayed with ResourceId as tooltip.
TABLE_COLUMNS = [
    "MasterResource",  # Master from relationships/oci-resource-relations.json (else resource name)
    "ResourceName",  # Display from relationships/oci-resource-names.json; ResourceId for tooltip/copy/link
    "ServiceCategory",
    "ServiceName",
    "ChargeDescription",
    "ResourceType",
    "BilledCost",
    "Region",
    "oci_CompartmentName",
    "OwnerCreatedBy",
    "OwnerCreatedDate",
    "OwnerCreatedAgeDays",
]

# Display labels for table headers
COLUMN_LABELS = {
    "MasterResource": "Master resource",
    "ResourceName": "Resource Name",
    "ServiceCategory": "Service",
    "ServiceName": "Service Name",
    "ChargeDescription": "Charge Description",
    "ResourceType": "Resource Type",
    "BilledCost": "Billed Cost",
    "Region": "Region",
    "oci_CompartmentName": "Compartment",
    "OwnerCreatedBy": "Owner Created By",
    "OwnerCreatedDate": "Owner Created Date",
    "OwnerCreatedAgeDays": "Age (days)",
}


def find_available_months(consolidated_base: Path) -> list[tuple[int, int]]:
    """Find all (year, month) with consolidated reports under consolidated_base. Returns sorted list."""
    months: set[tuple[int, int]] = set()
    # Monthly reports: Consolidated/YYYY/YYYY-MM.html
    for p in consolidated_base.glob("*/*.html"):
        m = re.match(r"^(\d{4})-(\d{2})\.html$", p.name)
        if m:
            try:
                months.add((int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass
    # Also from YYYY/MM/ daily structure
    for p in consolidated_base.glob("*/*"):
        if p.is_dir():
            parts = p.relative_to(consolidated_base).parts
            if len(parts) == 2:
                try:
                    months.add((int(parts[0]), int(parts[1])))
                except ValueError:
                    pass
    return sorted(months, key=lambda x: (x[0], x[1]))


def generate_html_dashboard(
    rows: list[dict],
    date_str: str,
    output_path: Path,
    daily_rows_filter_data: list[dict] | None = None,
) -> None:
    """Generate an HTML dashboard with cost chart per ServiceCategory and resources table.

    For monthly reports (date_str YYYY-MM), pass daily_rows_filter_data: list of
    { "date": "YYYY-MM-DD", "items": [ same shape as rowsFilterData per consolidated row that day ] }
    so the stacked cost-per-day bar chart matches chart filters, table column filters, and hide-zero
    (via resourceId+skuId keys aligned with the monthly table rows).
    """
    oci_console_prefixes = load_resource_type_console_prefixes()
    _filter_help_icon = get_filter_help_nav_icon_html()
    enrich_rows_with_resource_names(rows)
    enrich_rows_with_master_resource(rows)
    attach_master_console_links(rows, oci_console_prefixes)
    cost_data = cost_by_service_category(rows)
    comp_data = cost_by_compartment(rows)
    owner_data = cost_by_owner(rows)
    region_data = cost_by_region(rows)
    age_data = cost_by_age(rows)
    total_cost = sum(c for _, c in cost_data)
    currency = "USD" if get_usd_conversion_rate() is not None else ((rows[0].get("BillingCurrency") or "USD") if rows else "USD")

    # Chart.js data - Service
    labels = [html.escape(cat) for cat, _ in cost_data]
    values = [round(c, 2) for _, c in cost_data]
    # Chart.js data - Compartment
    comp_labels = [html.escape(c) for c, _ in comp_data]
    comp_values = [round(c, 2) for _, c in comp_data]
    # Chart.js data - Owner
    owner_labels = [html.escape(o) for o, _ in owner_data]
    owner_values = [round(c, 2) for _, c in owner_data]
    # Chart.js data - Region
    region_labels = [html.escape(r) for r, _ in region_data]
    region_values = [round(c, 2) for _, c in region_data]
    # Chart.js data - Age
    age_labels = [html.escape(a) for a, _ in age_data]
    age_values = [round(c, 2) for _, c in age_data]
    # Raw labels for dropdown (no html.escape - JS will escape when building options)
    raw_labels = [c for c, _ in cost_data]
    raw_comp_labels = [c for c, _ in comp_data]
    raw_owner_labels = [o for o, _ in owner_data]
    raw_region_labels = [r for r, _ in region_data]
    raw_age_labels = [a for a, _ in age_data]
    colors = [
        "#1a365d", "#2c5282", "#2b6cb0", "#3182ce", "#4299e1",
        "#63b3ed", "#90cdf4", "#bee3f8", "#ebf8ff", "#e0e7ff",
    ]
    comp_colors = [
        "#065f46", "#047857", "#059669", "#10b981", "#34d399",
        "#6ee7b7", "#a7f3d0", "#d1fae5", "#ecfdf5", "#064e3b",
    ]
    owner_colors = [
        "#78350f", "#92400e", "#a16207", "#ca8a04", "#eab308",
        "#facc15", "#fde047", "#fef08a", "#fef9c3", "#854d0e",
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

    rows_filter_data = [build_rows_filter_item(r) for r in rows]
    multi_resource_masters = masters_with_multiple_resources(rows)

    # Build dropdown options for each dimension
    def _dropdown_opts(items: list[tuple[str, float]]) -> str:
        opts = '<option value="">(All)</option>'
        for label, _ in items:
            opts += f'<option value="{html.escape(label)}">{html.escape(label)}</option>'
        return opts

    cat_dropdown = _dropdown_opts(cost_data)
    comp_dropdown = _dropdown_opts(comp_data)
    owner_dropdown = _dropdown_opts(owner_data)
    region_dropdown = _dropdown_opts(region_data)
    age_dropdown = _dropdown_opts(age_data)

    # Daily report nav: prev day, day dropdown, next day, month report
    daily_nav_html = ""
    daily_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if daily_match:
        year, month, day = int(daily_match.group(1)), int(daily_match.group(2)), int(daily_match.group(3))
        try:
            curr = date(year, month, day)
            prev_d = curr - timedelta(days=1)
            next_d = curr + timedelta(days=1)
            same_month_prev = prev_d.month == month
            same_month_next = next_d.month == month
            prev_url = f"../{prev_d.month:02d}/{prev_d.year:04d}-{prev_d.month:02d}-{prev_d.day:02d}.html" if not same_month_prev else f"{prev_d.year:04d}-{prev_d.month:02d}-{prev_d.day:02d}.html"
            next_url = f"../{next_d.month:02d}/{next_d.year:04d}-{next_d.month:02d}-{next_d.day:02d}.html" if not same_month_next else f"{next_d.year:04d}-{next_d.month:02d}-{next_d.day:02d}.html"
            month_url = f"../{year:04d}-{month:02d}.html"
            month_dir = output_path.parent
            existing_days = set()
            for f in month_dir.glob("????-??-??.html"):
                m = re.match(r"^\d{4}-\d{2}-(\d{2})\.html$", f.name)
                if m and int(m.group(1)) <= calendar.monthrange(year, month)[1]:
                    existing_days.add(int(m.group(1)))
            existing_days.add(day)  # current report we're generating
            day_opts = "".join(
                f'<option value="{year:04d}-{month:02d}-{d:02d}.html"' + (' selected' if d == day else '') + f'>{d}-{month:02d}</option>'
                for d in sorted(existing_days)
            )
            daily_nav_html = f'''
    <nav class="daily-nav" aria-label="Daily report navigation">
        <a href="{html.escape(prev_url)}" class="nav-icon" title="Previous day">&#9664;</a>
        <select class="nav-day-select" aria-label="Select day" onchange="if(this.value)location.href=this.value">
            {day_opts}
        </select>
        <a href="{html.escape(next_url)}" class="nav-icon" title="Next day">&#9654;</a>
        <a href="{html.escape(month_url)}" class="nav-icon" title="Month report">&#128197;</a>
        <span class="reset-all-filters nav-icon" title="Reset all filters">&#8635;</span>{_filter_help_icon}
    </nav>'''
        except (ValueError, OverflowError):
            pass

    # Monthly report nav: prev month, month dropdown, next month, reset
    monthly_nav_html = ""
    monthly_match = re.match(r"^(\d{4})-(\d{2})$", date_str)
    if monthly_match and not daily_match:
        year, month = int(monthly_match.group(1)), int(monthly_match.group(2))
        try:
            curr = date(year, month, 1)
            prev_d = curr - timedelta(days=1)
            next_d = (curr.replace(day=28) + timedelta(days=4)).replace(day=1)
            prev_url = f"../{prev_d.year:04d}/{prev_d.year:04d}-{prev_d.month:02d}.html" if prev_d.year != year else f"{prev_d.year:04d}-{prev_d.month:02d}.html"
            next_url = f"../{next_d.year:04d}/{next_d.year:04d}-{next_d.month:02d}.html" if next_d.year != year else f"{next_d.year:04d}-{next_d.month:02d}.html"
            month_dir = output_path.parent / f"{month:02d}"
            existing_days = set()
            for f in month_dir.glob("????-??-??.html"):
                m = re.match(r"^\d{4}-\d{2}-(\d{2})\.html$", f.name)
                if m and int(m.group(1)) <= calendar.monthrange(year, month)[1]:
                    existing_days.add(int(m.group(1)))
            day_opts = "".join(
                f'<option value="{month:02d}/{year:04d}-{month:02d}-{d:02d}.html">{d}-{month:02d}</option>'
                for d in sorted(existing_days)
            )
            monthly_nav_html = f'''
    <nav class="daily-nav" aria-label="Monthly report navigation">
        <a href="{html.escape(prev_url)}" class="nav-icon" title="Previous month">&#9664;</a>
        <select class="nav-day-select" aria-label="Select day" onchange="if(this.value)location.href=this.value">
            <option value="">Day</option>
            {day_opts}
        </select>
        <a href="{html.escape(next_url)}" class="nav-icon" title="Next month">&#9654;</a>
        <span class="reset-all-filters nav-icon" title="Reset all filters">&#8635;</span>{_filter_help_icon}
    </nav>'''
        except (ValueError, OverflowError):
            pass

    if not daily_nav_html and not monthly_nav_html:
        daily_nav_html = f'<nav class="daily-nav" aria-label="Report navigation"><span class="reset-all-filters nav-icon" title="Reset all filters">&#8635;</span>{_filter_help_icon}</nav>'

    top_nav_html = daily_nav_html or monthly_nav_html

    # Title with month and year
    if daily_match:
        year, month, day = int(daily_match.group(1)), int(daily_match.group(2)), int(daily_match.group(3))
        month_name = calendar.month_name[month]
        page_title = f"Daily Focus Report for {month_name} {day}, {year}"
    elif monthly_match:
        year, month = int(monthly_match.group(1)), int(monthly_match.group(2))
        month_name = calendar.month_name[month]
        page_title = f"Monthly Focus Report for {month_name} {year}"
    else:
        page_title = "Focus Report"

    monthly_daily_bar_html = ""
    if monthly_match and not daily_match and daily_rows_filter_data:
        month_name_bar = calendar.month_name[month]
        monthly_daily_bar_html = monthly_daily_bar_chart_section_html(
            f"Cost per day ({month_name_bar} {year}, stacked by Service Name)",
        )

    # Month dropdown: all available months across years
    consolidated_base = output_path.parent.parent if monthly_match else output_path.parent.parent.parent
    available_months = find_available_months(consolidated_base)
    month_dropdown_html = ""
    curr_year = year if (daily_match or monthly_match) else None
    curr_month = month if (daily_match or monthly_match) else None
    if available_months and curr_year is not None and curr_month is not None:
        month_opts = []
        for y, m in available_months:
            month_name_d = calendar.month_name[m]
            if monthly_match:
                href = f"{y:04d}-{m:02d}.html" if str(y) == output_path.parent.name else f"../{y:04d}/{y:04d}-{m:02d}.html"
            else:
                href = f"../../{y:04d}/{y:04d}-{m:02d}.html"
            is_curr = (y, m) == (curr_year, curr_month)
            month_opts.append((f"{month_name_d} {y}", href, is_curr))
        opts_str = "".join(
            f'<option value="{html.escape(href)}"{" selected" if is_curr else ""}>{html.escape(label)}</option>'
            for label, href, is_curr in month_opts
        )
        month_dropdown_html = f'''
    <div class="month-select-row">
        <label for="monthSelect" class="month-select-label">Month:</label>
        <select id="monthSelect" class="month-select" aria-label="Select month" onchange="if(this.value)location.href=this.value">
            {opts_str}
        </select>
    </div>'''
    # Relative path to Consolidated/users/ for owner links (daily: ../../users/, monthly: ../users/)
    users_base_path = "../../users/" if daily_match else "../users/" if monthly_match else "users/"
    # Top menu bar (Home, Monthly reports, Per-user dashboard)
    consolidated_dir = get_consolidated_dir_from_output(output_path)
    nav_base_path = base_path_from_output(output_path, consolidated_dir)
    monthly_report_path = find_latest_monthly_report(consolidated_dir) or "2026/2026-03.html"
    top_menu_bar = get_top_nav_html(nav_base_path, monthly_report_path)
    age_chart_jump_link_html = ""
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title} - {date_str}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
        body {{ margin: 0; padding: 0; background: #f8fafc; color: #1e293b; }}
        {get_top_nav_css()}
        {get_filter_help_modal_css()}
        .report-content {{ padding: 2rem; }}
        h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; color: #0f172a; }}
        .subtitle {{ color: #64748b; margin-bottom: 2rem; }}
        .charts-row {{ display: flex; flex-wrap: wrap; gap: 2rem; margin-bottom: 2rem; }}
        .daily-bar-chart-section {{ width: 100%; margin: 0 0 2rem; box-sizing: border-box; }}
        .daily-bar-chart-wrapper {{ width: 100%; height: 280px; background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); box-sizing: border-box; }}
        .chart-container {{ flex: 1; min-width: 240px; max-width: 320px; }}
        .chart-wrapper {{ background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        .chart-filter {{ margin-top: 0.75rem; }}
        .chart-filter select {{ width: 100%; padding: 0.35rem 0.5rem; font-size: 0.8rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .total-cost {{ font-size: 1.25rem; font-weight: 600; color: #0c4a6e; margin-bottom: 1rem; }}
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
        .compartment-cell {{ cursor: help; }}
        .resource-name-col {{ width: 22ch; max-width: 22ch; overflow: hidden; text-overflow: ellipsis; }}
        .owner-col {{ width: 20ch; max-width: 20ch; overflow: hidden; text-overflow: ellipsis; }}
        .owner-cell {{ cursor: help; }}
        .owner-link {{ color: #2563eb; text-decoration: none; cursor: pointer; }}
        .owner-link:hover {{ text-decoration: underline; }}
        .created-date-col {{ width: 16ch; max-width: 16ch; white-space: nowrap; }}
        .truncate-20-col {{ width: 20ch; max-width: 20ch; overflow: hidden; text-overflow: ellipsis; }}
        .truncate-cell {{ cursor: help; }}
        th.sortable {{ cursor: pointer; user-select: none; }}
        th.sortable:hover {{ background: #1e293b; }}
        th.sorted-asc::after {{ content: ' \\25B2'; font-size: 0.7em; opacity: 0.8; }}
        th.sorted-desc::after {{ content: ' \\25BC'; font-size: 0.7em; opacity: 0.8; }}
        .filter-row select {{ width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font-size: 0.875rem; border: 1px solid #e2e8f0; border-radius: 4px; background: white; }}
        .filter-row select:focus {{ outline: none; border-color: #0f172a; }}
        #resourcesTable {{ font-size: 0.8rem; font-stretch: condensed; letter-spacing: -0.01em; }}
        #resourcesTable td, #resourcesTable th {{ padding: 0.4rem 0.6rem; }}
        #resourcesTable .filter-row select,
        #resourcesTableGroupedMaster .filter-row select {{ font-size: 0.75rem; }}
        .treemap-wrapper {{ background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 1rem; }}
        .treemap-container {{ position: relative; width: 100%; height: 380px; background: #f8fafc; border-radius: 6px; }}
        .treemap-cell {{ position: absolute; border: 1px solid rgba(15,23,42,0.12); border-radius: 4px; padding: 6px; overflow: hidden; display: flex; flex-direction: column; justify-content: center; box-sizing: border-box; }}
        .treemap-cell:hover {{ border-color: #0f172a; z-index: 1; }}
        .treemap-cell-label {{ font-size: 0.8rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #0f172a; }}
        .treemap-cell-value {{ font-size: 0.7rem; color: #64748b; margin-top: 2px; }}
        .chart-title-row {{ display: flex; align-items: center; justify-content: space-between; margin: 0 0 1rem; font-size: 1rem; }}
        .treemap-toggle {{ cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0.2rem; line-height: 1; user-select: none; border-radius: 4px; }}
        .treemap-toggle:hover {{ opacity: 1; background: rgba(0,0,0,0.06); }}
        .treemap-toggle.active {{ opacity: 1; color: #0f172a; }}
        .reset-filter {{ cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0.2rem; line-height: 1; user-select: none; border-radius: 4px; margin-left: 0.25rem; }}
        .reset-filter:hover {{ opacity: 1; background: rgba(0,0,0,0.06); }}
        .treemap-section {{ display: none; }}
        .treemap-section.visible {{ display: block; margin-top: 2rem; margin-left: -2rem; margin-right: -2rem; width: calc(100% + 4rem); max-width: 100vw; box-sizing: border-box; }}
        .treemap-section.visible .treemap-wrapper {{ padding-left: 1rem; padding-right: 1rem; box-sizing: border-box; }}
        #treemapsBelowCharts {{ width: 100%; }}
        .daily-nav {{ display: flex; align-items: center; gap: 0.5rem; }}
        .nav-icon {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; font-size: 1.2rem; color: #0f172a; text-decoration: none; border-radius: 6px; background: #e2e8f0; }}
        .nav-icon:hover {{ background: #cbd5e1; color: #0f172a; }}
        .reset-all-filters {{ cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }}
        .nav-day-select {{ padding: 0.35rem 0.5rem; font-size: 0.9rem; border: 1px solid #e2e8f0; border-radius: 6px; background: white; min-width: 4rem; }}
        .header-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 0.5rem; flex-wrap: wrap; }}
        .header-row h1 {{ margin: 0; }}
        .month-select-row {{ margin-top: 0.5rem; display: flex; align-items: center; gap: 0.5rem; }}
        .month-select-label {{ font-size: 0.875rem; color: #64748b; }}
        .month-select {{ padding: 0.35rem 0.5rem; font-size: 0.9rem; border: 1px solid #e2e8f0; border-radius: 6px; background: white; min-width: 10rem; }}
        .table-tools-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 1.5rem; margin-bottom: 0.75rem; }}
        .table-view-toggle {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
        .hide-zero-cost-label {{ display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; color: #475569; cursor: pointer; user-select: none; }}
        .hide-zero-cost-label input {{ width: 1rem; height: 1rem; accent-color: #0f172a; cursor: pointer; }}
        .table-toggle-btn {{ padding: 0.4rem 0.75rem; font-size: 0.875rem; border: 1px solid #e2e8f0; border-radius: 6px; background: white; color: #64748b; cursor: pointer; }}
        .table-toggle-btn:hover {{ background: #f8fafc; color: #0f172a; border-color: #cbd5e1; }}
        .table-toggle-btn.active {{ background: #0f172a; color: white; border-color: #0f172a; }}
        .grouped-table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        .grouped-table th, .grouped-table td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }}
        .grouped-table th {{ background: #0f172a; color: white; }}
        @media (max-width: 768px) {{ table {{ font-size: 0.875rem; }} td, th {{ padding: 0.5rem; }} .treemap-container {{ height: 280px; }} }}
    </style>
</head>
<body>
{top_menu_bar}
    <div class="report-content">
    <div class="header-row">
        <div>
            <h1>{page_title}</h1>
            <p class="subtitle">Report date: {date_str} · {len(rows)} resources</p>
            {month_dropdown_html}
        </div>
        {top_nav_html}
    </div>
    <div class="total-cost" id="totalCost">Total cost: {total_cost:.2f} {currency}</div>
{age_chart_jump_link_html}
    <div class="charts-row">
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Service</h2><span><span class="treemap-toggle" data-treemap="treemapSectionServiceCategory" title="Show treemap">▦</span><span class="reset-filter" data-reset="cat" title="Reset Service filter">↺</span></span></div>
                <canvas id="costChart" width="400" height="300"></canvas>
                <div class="chart-filter"><select id="filterServiceCategory" aria-label="Filter by Service">""" + cat_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Compartment</h2><span><span class="treemap-toggle" data-treemap="treemapSectionCompartment" title="Show treemap">▦</span><span class="reset-filter" data-reset="comp" title="Reset Compartment filter">↺</span></span></div>
                <canvas id="compartmentChart" width="400" height="300"></canvas>
                <div class="chart-filter"><select id="filterCompartment" aria-label="Filter by Compartment">""" + comp_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Owner</h2><span><span class="treemap-toggle" data-treemap="treemapSectionOwner" title="Show treemap">▦</span><span class="reset-filter" data-reset="owner" title="Reset Owner filter">↺</span></span></div>
                <canvas id="ownerChart" width="400" height="300"></canvas>
                <div class="chart-filter"><select id="filterOwner" aria-label="Filter by Owner">""" + owner_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Cost by Region</h2><span><span class="treemap-toggle" data-treemap="treemapSectionRegion" title="Show treemap">▦</span><span class="reset-filter" data-reset="region" title="Reset Region filter">↺</span></span></div>
                <canvas id="regionChart" width="400" height="300"></canvas>
                <div class="chart-filter"><select id="filterRegion" aria-label="Filter by Region">""" + region_dropdown + """</select></div>
            </div>
        </div>
        <div class="chart-container" id="ageChartSection">
            <div class="chart-wrapper">
                <div class="chart-title-row"><h2 style="margin: 0; font-size: 1rem;">Age</h2><span><span class="treemap-toggle" data-treemap="treemapSectionAge" title="Show treemap">▦</span><span class="reset-filter" data-reset="age" title="Reset Age filter">↺</span></span></div>
                <canvas id="ageChart" width="400" height="300"></canvas>
                <div class="chart-filter"><select id="filterAge" aria-label="Filter by Age">""" + age_dropdown + """</select></div>
            </div>
        </div>
    </div>
""" + monthly_daily_bar_html + """
    <div id="treemapsBelowCharts" style="margin-top: 2rem;">
        <div class="treemap-section" id="treemapSectionServiceCategory">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Service</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapServiceCategory"></div></div>
        </div>
        <div class="treemap-section" id="treemapSectionCompartment">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Compartment</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapCompartment"></div></div>
        </div>
        <div class="treemap-section" id="treemapSectionOwner">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Owner</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapOwner"></div></div>
        </div>
        <div class="treemap-section" id="treemapSectionRegion">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Cost by Region</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapRegion"></div></div>
        </div>
        <div class="treemap-section" id="treemapSectionAge">
            <h2 style="margin: 0 0 1rem; font-size: 1rem;">Age</h2>
            <div class="treemap-wrapper"><div class="treemap-container" id="treemapAge"></div></div>
        </div>
    </div>

    <h2 style="margin: 2rem 0 1rem; font-size: 1rem;">All Resources</h2>
    <div class="table-tools-row">
    <div class="table-view-toggle">
        <button type="button" class="table-toggle-btn" data-mode="all" aria-pressed="false">All resources</button>
        <button type="button" class="table-toggle-btn" data-mode="groupedService" aria-pressed="false">Grouped by Service Name</button>
        <button type="button" class="table-toggle-btn" data-mode="groupedResourceType" aria-pressed="false">Grouped by Resource Type</button>
        <button type="button" class="table-toggle-btn active" data-mode="groupedMaster" aria-pressed="true">Grouped by Master Resource</button>
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
    for i, col in enumerate(TABLE_COLUMNS):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "sortable resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "sortable owner-col"
                if col == "OwnerCreatedBy"
                else (
                    "sortable created-date-col"
                    if col == "OwnerCreatedDate"
                    else ("sortable truncate-20-col" if col in truncate_cols else "sortable")
                )
            )
        )
        html_content += f"                <th class=\"{th_class}\" data-col=\"{i}\">{html.escape(label)}</th>\n"
    html_content += """            </tr>
            <tr class="filter-row">
"""
    # Build unique values per column for dropdowns
    col_unique: dict[int, set[str]] = {i: set() for i in range(len(TABLE_COLUMNS))}
    for row in rows:
        for i, col in enumerate(TABLE_COLUMNS):
            if col == "ResourceName":
                val = (row.get("ResourceName") or "").strip()
                if not val:
                    val = (row.get("ResourceId") or "").strip()
            elif col == "MasterResource":
                val = (row.get("MasterResource") or "").strip()
            elif col == "OwnerCreatedDate":
                val = format_created_date_display(row.get(col, "") or "")
            else:
                val = row.get(col, "")
                if col in ("BilledCost",):
                    try:
                        cv = float(val or 0)
                        val = f"{to_display_cost(cv):.2f}"
                    except (ValueError, TypeError):
                        val = str(val)
            col_unique[i].add(str(val).strip() if val else "")
    EMPTY_SENTINEL = "__empty__"
    for i, col in enumerate(TABLE_COLUMNS):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        unique_vals = sorted(col_unique[i], key=lambda v: (v == "", v.lower() if isinstance(v, str) else str(v)))
        opts = "".join(
            f'<option value="{html.escape(v) if v else EMPTY_SENTINEL}">{html.escape(v) if v else "(empty)"}</option>'
            for v in unique_vals
        )
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "owner-col"
                if col == "OwnerCreatedBy"
                else (
                    "created-date-col"
                    if col == "OwnerCreatedDate"
                    else ("truncate-20-col" if col in truncate_cols else "")
                )
            )
        )
        html_content += f"                <th class=\"{th_class}\"><select data-col=\"{i}\" aria-label=\"Filter {html.escape(label)}\"><option value=\"__all__\">(All)</option>{opts}</select></th>\n"
    html_content += """            </tr>
        </thead>
        <tbody>
"""
    for i, row in enumerate(rows):
        fd = rows_filter_data[i] if i < len(rows_filter_data) else {}
        tr_attrs = f' data-service-category="{html.escape(fd.get("serviceCategory", ""))}" data-compartment="{html.escape(fd.get("compartment", ""))}" data-owner="{html.escape(fd.get("owner", ""))}" data-region="{html.escape(fd.get("region", ""))}" data-age="{html.escape(fd.get("age", ""))}"'
        html_content += f"            <tr{tr_attrs}>\n"
        for col in TABLE_COLUMNS:
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
                html_content += f"                <td class=\"resource-name-col\" data-value=\"{html.escape(full_name)}\">{cell_content}</td>\n"
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
                deleted_icon = deleted_resource_icon_html(display)
                cell_content = f'{console_link}{deleted_icon}<span class="resource-name"{tooltip}>{html.escape(display_short)}</span>{copy_btn}'
                html_content += f"                <td class=\"resource-name-col\" data-value=\"{html.escape(display)}\">{cell_content}</td>\n"
            elif col == "oci_CompartmentName":
                comp_name = (val or "").strip() or (row.get("oci_CompartmentPath") or "").strip() or "—"
                comp_path = (row.get("oci_CompartmentPath") or "").strip()
                tooltip = f' title="{html.escape(comp_path)}"' if comp_path else ""
                cell_content = f'<span class="compartment-cell"{tooltip}>{html.escape(comp_name)}</span>'
                html_content += f"                <td data-value=\"{html.escape(comp_name)}\">{cell_content}</td>\n"
            elif col == "OwnerCreatedBy":
                full_owner = (val or "").strip() or "(No owner)"
                display_short = (full_owner[:20] + "…") if len(full_owner) > 20 else full_owner
                tooltip = f' title="{html.escape(full_owner)}"'
                safe_name = sanitize_owner_filename(full_owner)
                owner_href = f"{users_base_path}{html.escape(safe_name)}.html"
                cell_content = f'<a href="{owner_href}" class="owner-cell owner-link"{tooltip}>{html.escape(display_short)}</a>'
                html_content += f"                <td class=\"owner-col\" data-value=\"{html.escape(full_owner)}\">{cell_content}</td>\n"
            elif col == "OwnerCreatedDate":
                display_val = format_created_date_display(val or "")
                html_content += f"                <td class=\"created-date-col\" data-value=\"{html.escape(display_val or str(val))}\">{html.escape(display_val or '—')}</td>\n"
            elif col in ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType"):
                full_text = (val or "").strip() or "—"
                display_short = (full_text[:20] + "…") if len(full_text) > 20 else full_text
                tooltip = f' title="{html.escape(full_text)}"'
                cell_content = f'<span class="truncate-cell"{tooltip}>{html.escape(display_short)}</span>'
                html_content += f"                <td class=\"truncate-20-col\" data-value=\"{html.escape(full_text)}\">{cell_content}</td>\n"
            else:
                if col in ("BilledCost",):
                    try:
                        cv = float(val or 0)
                        val = f"{to_display_cost(cv):.2f}"
                    except (ValueError, TypeError):
                        val = str(val)
                cell_class = "num" if col in ("BilledCost", "OwnerCreatedAgeDays") else ""
                html_content += f"                <td{' class=\"' + cell_class + '\"' if cell_class else ''} data-value=\"{html.escape(str(val))}\">{html.escape(str(val))}</td>\n"
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
    for i, col in enumerate(TABLE_COLUMNS):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "sortable resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "sortable owner-col"
                if col == "OwnerCreatedBy"
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
        )
        html_content += f"                <th class=\"{th_class}\" data-col=\"{i}\">{html.escape(label)}</th>\n"
    html_content += """            </tr>
            <tr class="filter-row">
"""
    for i, col in enumerate(TABLE_COLUMNS):
        label = COLUMN_LABELS.get(col, col.replace("oci_", "").replace("_", " ").title())
        unique_vals = sorted(col_unique[i], key=lambda v: (v == "", v.lower() if isinstance(v, str) else str(v)))
        opts = "".join(
            f'<option value="{html.escape(v) if v else EMPTY_SENTINEL}">{html.escape(v) if v else "(empty)"}</option>'
            for v in unique_vals
        )
        truncate_cols = ("ServiceCategory", "ServiceName", "ChargeDescription", "ResourceType")
        th_class = (
            "resource-name-col"
            if col in ("ResourceName", "MasterResource")
            else (
                "owner-col"
                if col == "OwnerCreatedBy"
                else (
                    "created-date-col"
                    if col == "OwnerCreatedDate"
                    else ("truncate-20-col" if col in truncate_cols else "")
                )
            )
        )
        html_content += f"                <th class=\"{th_class}\"><select data-col=\"{i}\" aria-label=\"Filter {html.escape(label)}\"><option value=\"__all__\">(All)</option>{opts}</select></th>\n"
    html_content += """            </tr>
        </thead>
        <tbody></tbody>
    </table>
    </div>

    <script>
        (function() {
            const rowsFilterData = """ + json.dumps(rows_filter_data) + """;
            const currency = """ + json.dumps(currency) + """;
            const colors = """ + json.dumps(colors) + """;
            const compColors = """ + json.dumps(comp_colors) + """;
            const ownerColors = """ + json.dumps(owner_colors) + """;
            const regionColors = """ + json.dumps(region_colors) + """;
            const ageColors = """ + json.dumps(age_colors) + """;
            const ageBucketToColor = """ + json.dumps(age_bucket_to_color) + """;
            const totalCostEl = document.getElementById('totalCost');
            const table = document.getElementById('resourcesTable');
            const tbody = table.querySelector('tbody');
            const headers = table.querySelectorAll('th.sortable');
            const filtersAll = Array.from(table.querySelectorAll('.filter-row select'));
            const chartFilterCat = document.getElementById('filterServiceCategory');
            const chartFilterComp = document.getElementById('filterCompartment');
            const chartFilterOwner = document.getElementById('filterOwner');
            const chartFilterRegion = document.getElementById('filterRegion');
            const chartFilterAge = document.getElementById('filterAge');
            const BILLED_COST_COL = """ + str(TABLE_COLUMNS.index("BilledCost")) + """;
            const SERVICE_NAME_COL = """ + str(TABLE_COLUMNS.index("ServiceName")) + """;
            const MASTER_RESOURCE_COL = """ + str(TABLE_COLUMNS.index("MasterResource")) + """;
            const RESOURCE_NAME_COL = """ + str(TABLE_COLUMNS.index("ResourceName")) + """;
            let sortCol = BILLED_COST_COL, sortAsc = true;
            let groupedSortCol = 2, groupedSortAsc = false;
            let masterGroupedSortCol = BILLED_COST_COL, masterGroupedSortAsc = false;
            let tableMode = 'groupedMaster';
            const tableAllContainer = document.getElementById('tableAllContainer');
            const tableGroupedContainer = document.getElementById('tableGroupedContainer');
            const tableGroupedMasterContainer = document.getElementById('tableGroupedMasterContainer');
            const tbodyGrouped = document.getElementById('resourcesTableGrouped')?.querySelector('tbody');
            const tableGroupedMaster = document.getElementById('resourcesTableGroupedMaster');
            const tbodyGroupedMaster = tableGroupedMaster?.querySelector('tbody');
            const filtersMaster = Array.from(tableGroupedMaster?.querySelectorAll('.filter-row select') || []);
            const hideZeroCostEl = document.getElementById('hideZeroCostRows');
            const excluded = { cat: new Set(), comp: new Set(), owner: new Set(), region: new Set(), age: new Set() };
            const dailyRowsFilterData = """ + json.dumps(daily_rows_filter_data or []) + """;
            const dailyBarColors = """ + json.dumps(DAILY_BAR_CHART_COLORS) + """;
            const EMPTY_SENTINEL = '__empty__';
""" + get_combined_items_js_helpers() + """
            function syncColumnFilters(fromList, toList) {
                fromList.forEach((f, i) => { if (toList[i]) toList[i].value = f.value; });
            }
            function getColumnFilterVals() {
                const src = (tableMode === 'groupedMaster' && filtersMaster.length) ? filtersMaster : filtersAll;
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
                    plugins: {
                        legend: { display: false },
                        tooltip: { enabled: true }
                    }
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
                        datasets: [{
                            data: values,
                            backgroundColor: values.map((_, i) => colorArr[i % colorArr.length]),
                            borderWidth: 0
                        }]
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
            const costChart = createChart('costChart', """ + json.dumps(labels) + """, """ + json.dumps(values) + """, colors, chartFilterCat, 'cat');
            const compChart = createChart('compartmentChart', """ + json.dumps(comp_labels) + """, """ + json.dumps(comp_values) + """, compColors, chartFilterComp, 'comp');
            const ownerChart = createChart('ownerChart', """ + json.dumps(owner_labels) + """, """ + json.dumps(owner_values) + """, ownerColors, chartFilterOwner, 'owner');
            const regionChart = createChart('regionChart', """ + json.dumps(region_labels) + """, """ + json.dumps(region_values) + """, regionColors, chartFilterRegion, 'region');
            const ageChart = createChart('ageChart', """ + json.dumps(age_labels) + """, """ + json.dumps(age_values) + """, ageColors, chartFilterAge, 'age');
            let monthlyDailyBarChart = null;

            function rowMatchesChartFilters(r) {
                const catVal = chartFilterCat?.value || '';
                const compVal = chartFilterComp?.value || '';
                const ownerVal = chartFilterOwner?.value || '';
                const regionVal = chartFilterRegion?.value || '';
                const ageVal = chartFilterAge?.value || '';
                if (excluded.cat.has(r.serviceCategory)) return false;
                if (excluded.comp.has(r.compartment)) return false;
                if (excluded.owner.has(r.owner)) return false;
                if (excluded.region.has(r.region)) return false;
                if (excluded.age.has(r.age)) return false;
                if (catVal && r.serviceCategory !== catVal) return false;
                if (compVal && r.compartment !== compVal) return false;
                if (ownerVal && r.owner !== ownerVal) return false;
                if (regionVal && r.region !== regionVal) return false;
                if (ageVal && r.age !== ageVal) return false;
                return true;
            }

            function rowMatchesTableColumnFilters(r, filterVals) {
                if (!r.tableCols || !r.tableCols.length) return true;
                for (let j = 0; j < filterVals.length; j++) {
                    if (filterVals[j] && filterVals[j] !== '__all__') {
                        let cellVal = r.tableCols[j] != null ? String(r.tableCols[j]) : '';
                        cellVal = (cellVal === '' || cellVal == null) ? EMPTY_SENTINEL : cellVal;
                        const fv = filterVals[j] === EMPTY_SENTINEL ? EMPTY_SENTINEL : filterVals[j];
                        if (cellVal !== fv) return false;
                    }
                }
                return true;
            }

            function buildVisibleResourceKeysForDailyBar() {
                const filterVals = getColumnFilterVals();
                const hideZero = !!(hideZeroCostEl && hideZeroCostEl.checked);
                const hasTableCols = rowsFilterData.length && rowsFilterData[0].tableCols != null;
                const keys = new Set();
                rowsFilterData.forEach((r) => {
                    if (!rowMatchesChartFilters(r)) return;
                    if (hasTableCols && !rowMatchesTableColumnFilters(r, filterVals)) return;
                    if (hideZero && isRoundedZeroCost(r.billedCost)) return;
                    keys.add(`${r.resourceId || ''}|${r.skuId || ''}`);
                });
                return keys;
            }

            function updateDailyBarChart() {
                const canvas = document.getElementById('monthlyDailyBarChart');
                if (!canvas || !dailyRowsFilterData.length) return;
                const visibleKeys = buildVisibleResourceKeysForDailyBar();
                const firstItem = dailyRowsFilterData[0].items && dailyRowsFilterData[0].items[0];
                const useKeyFilter = firstItem && firstItem.resourceId != null;
                const serviceTotals = {};
                const labels = [];
                const perDayBySvc = dailyRowsFilterData.map((day) => {
                    labels.push(day.date);
                    const bySvc = {};
                    (day.items || []).forEach((r) => {
                        if (useKeyFilter) {
                            const key = `${r.resourceId || ''}|${r.skuId || ''}`;
                            if (!visibleKeys.has(key)) return;
                        } else if (!rowMatchesChartFilters(r)) {
                            return;
                        }
                        const svc = r.serviceName || '(Uncategorized)';
                        const c = r.billedCost || 0;
                        bySvc[svc] = (bySvc[svc] || 0) + c;
                        serviceTotals[svc] = (serviceTotals[svc] || 0) + c;
                    });
                    return bySvc;
                });
                const serviceOrder = Object.keys(serviceTotals).sort((a, b) => serviceTotals[b] - serviceTotals[a]);
                const datasets = serviceOrder.map((svc, i) => ({
                    label: svc,
                    data: perDayBySvc.map((bySvc) => Math.round((bySvc[svc] || 0) * 100) / 100),
                    backgroundColor: dailyBarColors[i % dailyBarColors.length],
                    borderWidth: 0,
                }));
                const ctx = canvas.getContext('2d');
                const barOpts = {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            mode: 'index',
                            callbacks: {
                                label: (ctx) => {
                                    const y = (ctx.parsed && ctx.parsed.y !== undefined) ? ctx.parsed.y : ctx.raw;
                                    return y > 0 ? ctx.dataset.label + ': ' + Number(y).toFixed(2) + ' ' + currency : '';
                                },
                            },
                        },
                    },
                    scales: {
                        x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 45, minRotation: 45 } },
                        y: { stacked: true, beginAtZero: true, ticks: { callback: (v) => v + ' ' + currency } },
                    },
                };
                if (monthlyDailyBarChart) {
                    monthlyDailyBarChart.data.labels = labels;
                    monthlyDailyBarChart.data.datasets = datasets;
                    monthlyDailyBarChart.update();
                } else {
                    monthlyDailyBarChart = new Chart(ctx, { type: 'bar', data: { labels, datasets }, options: barOpts });
                }
            }

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
                const byCat = {}, byComp = {}, byOwner = {}, byRegion = {}, byAge = {};
                let total = 0;
                filtered.forEach(r => {
                    const c = r.billedCost || 0;
                    total += c;
                    byCat[r.serviceCategory] = (byCat[r.serviceCategory] || 0) + c;
                    byComp[r.compartment] = (byComp[r.compartment] || 0) + c;
                    byOwner[r.owner] = (byOwner[r.owner] || 0) + c;
                    byRegion[r.region] = (byRegion[r.region] || 0) + c;
                    byAge[r.age] = (byAge[r.age] || 0) + c;
                });
                const ageOrder = ['<7 days','<31 days','<6 months','<1 year','1 to 2 years','2+ years','(Unknown age)'];
                return {
                    cat: Object.entries(byCat).sort((a,b)=>b[1]-a[1]),
                    comp: Object.entries(byComp).sort((a,b)=>b[1]-a[1]),
                    owner: Object.entries(byOwner).sort((a,b)=>b[1]-a[1]),
                    region: Object.entries(byRegion).sort((a,b)=>b[1]-a[1]),
                    age: Object.entries(byAge).sort((a,b)=>(ageOrder.indexOf(a[0])-ageOrder.indexOf(b[0]))||(b[1]-a[1])),
                    total
                };
            }

            function updateSummaryTable(tbodyId, data, total) {
                const tbody = document.getElementById(tbodyId);
                if (!tbody) return;
                tbody.innerHTML = data.map(([label, cost]) => {
                    const pct = total ? (cost/total*100).toFixed(1) : 0;
                    return `<tr><td>${label.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}</td><td class="num">${cost.toFixed(2)}</td><td class="num">${pct}%</td></tr>`;
                }).join('');
            }

            function squarify(data, x, y, w, h) {
                const values = data.map(([,v]) => Math.max(0, v));
                const total = values.reduce((a,b)=>a+b,0);
                if (total <= 0 || data.length === 0) return [];
                const area = w * h;
                const norm = values.map(v => (v/total)*area);
                const result = [];
                let idx = 0;
                const sumArr = a => a.reduce((s,v)=>s+v,0);
                const worstRatio = (row, length) => {
                    const s = sumArr(row), mn = Math.min(...row), mx = Math.max(...row);
                    return Math.max(Math.pow(length,2)*mx/Math.pow(s,2), Math.pow(s,2)/(Math.pow(length,2)*mn));
                };
                while (idx < norm.length) {
                    let row = [], worst = Infinity;
                    let j = idx;
                    while (j < norm.length) {
                        const next = row.concat([norm[j]]);
                        const r = worstRatio(next, next.length);
                        if (row.length === 0 || r <= worst) { row = next; worst = r; j++; }
                        else break;
                    }
                    const rowSum = sumArr(row);
                    const horizontal = w >= h;
                    const rowSize = horizontal ? rowSum / h : rowSum / w;
                    let cx = x, cy = y;
                    for (let k = 0; k < row.length; k++) {
                        const a = row[k];
                        const rw = horizontal ? rowSize : a / rowSize;
                        const rh = horizontal ? a / rowSize : rowSize;
                        const [label, cost] = data[idx+k];
                        result.push({ label, cost, x: cx, y: cy, w: rw, h: rh });
                        if (horizontal) cy += rh; else cx += rw;
                    }
                    if (horizontal) { x += rowSize; w -= rowSize; } else { y += rowSize; h -= rowSize; }
                    idx = j;
                }
                return result;
            }

            function updateTreemap(containerId, data, total, colorArr, currency) {
                const el = document.getElementById(containerId);
                if (!el) return;
                if (!data.length) { el.innerHTML = ''; return; }
                data = data.filter(([, cost]) => total > 0 && (cost / total) >= 0.01);
                if (!data.length) { el.innerHTML = ''; return; }
                const pad = 4;
                const W = Math.max(100, (el.offsetWidth || el.clientWidth || 600) - pad*2);
                const H = Math.max(100, (el.offsetHeight || el.clientHeight || 372) - pad*2);
                const rects = squarify(data, 0, 0, W, H);
                el.innerHTML = rects.map((r, i) => {
                    const { label, cost } = r;
                    const pct = total ? (cost/total*100).toFixed(1) : 0;
                    const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                    const color = Array.isArray(colorArr)
                        ? colorArr[i % colorArr.length]
                        : (colorArr[label] || colorArr['(Unknown age)'] || '#94a3b8');
                    return `<div class="treemap-cell" style="left:${pad+r.x}px;top:${pad+r.y}px;width:${Math.max(0,r.w-1)}px;height:${Math.max(0,r.h-1)}px;background:${color};" title="${esc(label)}: ${cost.toFixed(2)} ${currency} (${pct}%)"><span class="treemap-cell-label">${esc(label)}</span><span class="treemap-cell-value">${cost.toFixed(2)} ${currency} (${pct}%)</span></div>`;
                }).join('');
            }

            function applyAllFilters() {
                updateDropdownStrikethrough();
                const catVal = chartFilterCat?.value || '';
                const compVal = chartFilterComp?.value || '';
                const ownerVal = chartFilterOwner?.value || '';
                const regionVal = chartFilterRegion?.value || '';
                const ageVal = chartFilterAge?.value || '';
                const filtered = rowsFilterData.filter(r => rowMatchesChartFilters(r));
                const agg = aggregateFiltered(filtered);
                totalCostEl.textContent = `Total cost: ${agg.total.toFixed(2)} ${currency}`;
                updateTreemap('treemapServiceCategory', agg.cat, agg.total, colors, currency);
                updateTreemap('treemapCompartment', agg.comp, agg.total, compColors, currency);
                updateTreemap('treemapOwner', agg.owner, agg.total, ownerColors, currency);
                updateTreemap('treemapRegion', agg.region, agg.total, regionColors, currency);
                updateTreemap('treemapAge', agg.age, agg.total, ageBucketToColor, currency);
                updateChart(costChart, agg.cat, colors);
                updateChart(compChart, agg.comp, compColors);
                updateChart(ownerChart, agg.owner, ownerColors);
                updateChart(regionChart, agg.region, regionColors);
                updateChart(ageChart, agg.age, ageBucketToColor);
                updateDailyBarChart();
                const filterVals = getColumnFilterVals();
                const hideZero = !!(hideZeroCostEl && hideZeroCostEl.checked);
                const rows = tbody.querySelectorAll('tr');
                function applyTableRowVisibility() {
                    rows.forEach((row) => {
                        const svcCat = row.getAttribute('data-service-category') || '';
                        const comp = row.getAttribute('data-compartment') || '';
                        const owner = row.getAttribute('data-owner') || '';
                        const region = row.getAttribute('data-region') || '';
                        const age = row.getAttribute('data-age') || '';
                        let chartMatch = true;
                        if (excluded.cat.has(svcCat)) chartMatch = false;
                        if (excluded.comp.has(comp)) chartMatch = false;
                        if (excluded.owner.has(owner)) chartMatch = false;
                        if (excluded.region.has(region)) chartMatch = false;
                        if (excluded.age.has(age)) chartMatch = false;
                        if (catVal && svcCat !== catVal) chartMatch = false;
                        if (compVal && comp !== compVal) chartMatch = false;
                        if (ownerVal && owner !== ownerVal) chartMatch = false;
                        if (regionVal && region !== regionVal) chartMatch = false;
                        if (ageVal && age !== ageVal) chartMatch = false;
                        let colMatch = true;
                        for (let j = 0; j < filterVals.length; j++) {
                            if (filterVals[j] && filterVals[j] !== '__all__') {
                                let cellVal = getCellValue(row, j);
                                cellVal = (cellVal === '' || cellVal == null) ? EMPTY_SENTINEL : cellVal;
                                const fv = filterVals[j] === EMPTY_SENTINEL ? EMPTY_SENTINEL : filterVals[j];
                                if (cellVal !== fv) { colMatch = false; break; }
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
                if (tableMode === 'groupedMaster' && tbodyGroupedMaster) {
                    tableAllContainer.style.display = 'none';
                    tableGroupedContainer.style.display = 'none';
                    tableGroupedMasterContainer.style.display = '';
                    const hasTableCols = rowsFilterData.length && rowsFilterData[0].tableCols != null;
                    const filteredForMaster = hasTableCols
                        ? filtered.filter(r => rowMatchesTableColumnFilters(r, filterVals))
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
                            masterConsoleLink: masterItem.masterConsoleLink || ''
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
                                shown = deletedResourceIconHtml(v) + shown;
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
                    const col = groupedSortCol;
                    const asc = groupedSortAsc;
                    groupedRows.sort((a, b) => {
                        let cmp;
                        if (col === 0) cmp = String(a[0]).localeCompare(String(b[0]), undefined, { numeric: true });
                        else if (col === 1) cmp = a[1].count - b[1].count;
                        else cmp = a[1].cost - b[1].cost;
                        return asc ? cmp : -cmp;
                    });
                    const groupedHeaders = document.querySelectorAll('#resourcesTableGrouped th.sortable');
                    groupedHeaders.forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                    if (groupedHeaders[col]) groupedHeaders[col].classList.add(asc ? 'sorted-asc' : 'sorted-desc');
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
            }

            function applyFilters() { applyAllFilters(); }

            function sort(col) {
                sortAsc = (sortCol === col) ? !sortAsc : true;
                sortCol = col;
                headers.forEach((h, i) => h.classList.remove('sorted-asc', 'sorted-desc'));
                headers[col].classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.sort((a, b) => {
                    const va = getCellValue(a, col);
                    const vb = getCellValue(b, col);
                    const na = parseFloat(va), nb = parseFloat(vb);
                    let cmp;
                    if (!isNaN(na) && !isNaN(nb)) cmp = na - nb;
                    else cmp = String(va).localeCompare(String(vb), undefined, { numeric: true });
                    return sortAsc ? cmp : -cmp;
                });
                rows.forEach(r => tbody.appendChild(r));
            }

            tbody.addEventListener('click', (e) => {
                const btn = e.target.closest('.copy-resource-btn');
                if (btn) {
                    const text = btn.getAttribute('data-copy');
                    if (text) {
                        navigator.clipboard.writeText(text).then(() => {
                            const orig = btn.textContent;
                            btn.textContent = '✓';
                            setTimeout(() => { btn.textContent = orig; }, 800);
                        });
                    }
                }
            });
            headers.forEach((h, i) => h.addEventListener('click', () => sort(i)));
            filtersAll.forEach(f => f.addEventListener('change', () => onColumnFilterChange('all')));
            filtersMaster.forEach(f => f.addEventListener('change', () => onColumnFilterChange('master')));
            [chartFilterCat, chartFilterComp, chartFilterOwner, chartFilterRegion, chartFilterAge].forEach(el => {
                if (el) el.addEventListener('change', applyAllFilters);
            });
            document.querySelectorAll('.treemap-toggle').forEach(btn => {
                btn.addEventListener('click', () => {
                    const sectionId = btn.getAttribute('data-treemap');
                    const section = document.getElementById(sectionId);
                    if (!section) return;
                    const wasVisible = section.classList.contains('visible');
                    document.querySelectorAll('.treemap-section').forEach(s => s.classList.remove('visible'));
                    document.querySelectorAll('.treemap-toggle').forEach(b => b.classList.remove('active'));
                    if (!wasVisible) {
                        section.classList.add('visible');
                        btn.classList.add('active');
                        section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                        requestAnimationFrame(() => { requestAnimationFrame(() => applyAllFilters()); });
                    }
                });
            });
            const chartFilterLabels = { cat: """ + json.dumps(raw_labels) + """, comp: """ + json.dumps(raw_comp_labels) + """, owner: """ + json.dumps(raw_owner_labels) + """, region: """ + json.dumps(raw_region_labels) + """, age: """ + json.dumps(raw_age_labels) + """ };
            const chartFilterEls = { cat: chartFilterCat, comp: chartFilterComp, owner: chartFilterOwner, region: chartFilterRegion, age: chartFilterAge };
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
            document.querySelectorAll('.reset-filter').forEach(btn => {
                btn.addEventListener('click', () => {
                    const key = btn.getAttribute('data-reset');
                    if (key && excluded[key]) { excluded[key].clear(); }
                    const el = chartFilterEls[key];
                    if (el) el.value = '';
                    updateDropdownStrikethrough();
                    applyAllFilters();
                });
            });
            document.querySelectorAll('.reset-all-filters').forEach(btn => {
                btn.addEventListener('click', () => {
                    excluded.cat.clear(); excluded.comp.clear(); excluded.owner.clear(); excluded.region.clear(); excluded.age.clear();
                    [chartFilterCat, chartFilterComp, chartFilterOwner, chartFilterRegion, chartFilterAge].forEach(el => { if (el) el.value = ''; });
                    filtersAll.forEach(f => { f.value = '__all__'; });
                    filtersMaster.forEach(f => { f.value = '__all__'; });
                    updateDropdownStrikethrough();
                    applyAllFilters();
                });
            });
            if (hideZeroCostEl) hideZeroCostEl.addEventListener('change', applyAllFilters);
            document.querySelectorAll('.table-toggle-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const mode = btn.getAttribute('data-mode');
                    if (!mode) return;
                    const prevMode = tableMode;
                    if (prevMode === 'groupedMaster' && mode !== 'groupedMaster') syncColumnFilters(filtersMaster, filtersAll);
                    if (mode === 'groupedMaster' && prevMode !== 'groupedMaster') syncColumnFilters(filtersAll, filtersMaster);
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
                    if (tableMode === 'groupedMaster') applyAllFilters();
                });
            });
            applyAllFilters();
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


def consolidate_reports(
    base_dir: str,
    output_dir: str | None = None,
    force: bool = False,
    update: bool = False,
    anon: bool = False,
) -> None:
    """
    Process all FOCUS report CSVs under base_dir and write daily consolidated reports.
    Output CSV: {output_dir}/ConsolidatedCSV/YYYY/MM/YYYY-MM-DD.csv
    Output HTML: {output_dir}/ConsolidatedHTML/YYYY/MM/YYYY-MM-DD.html

    When update=True: only process days where focus inputs are newer than consolidated output (or output missing),
    and only within the configured stale-check window (see dailyReportStaleCheckDays); older days with existing outputs are ignored.
    If daily reports change for a month, also update the monthly report.
    When force=True: process all reports (overrides update and stale window).
    When not update and not force: skip a day only if consolidated CSV+HTML exist and are up to date vs all focus inputs (mtime checks only within the stale-check window).
    """
    output_root = Path(output_dir or base_dir)
    csv_base = output_root / "ConsolidatedCSV"
    html_base = output_root / "ConsolidatedHTML"

    anon_state: dict | None = None
    if anon:
        anon_state = load_anon_mapping(output_root)
        if anon_state["mapping"]:
            print(f"(anon mode: loaded {len(anon_state['mapping'])} existing mapping(s), building incrementally)")
        else:
            print("(anon mode: building mapping incrementally while processing)")

    by_date = find_focus_report_csvs(base_dir)
    if not by_date:
        print(f"No CSV files found under {base_dir}")
        return

    stale_cutoff = stale_check_cutoff_date()
    stale_days = get_daily_report_stale_check_days()
    if stale_cutoff is not None:
        print(
            f"Daily stale-check window: last {stale_days} day(s), dates >= {stale_cutoff.isoformat()} "
            f"(dailyReportStaleCheckDays in config.json; 0 = check all days)"
        )
    else:
        print("Daily stale-check window: all days (dailyReportStaleCheckDays <= 0 in config.json)")

    def _day_in_stale_window(y: int, m: int, d: int) -> bool:
        if stale_cutoff is None:
            return True
        try:
            return date(y, m, d) >= stale_cutoff
        except ValueError:
            return False

    # When -update: only days where focus inputs are newer than consolidated output (or output missing),
    # and only within stale window (older days with both outputs already are ignored)
    if update and not force:
        filtered: dict[tuple[int, int, int], list[tuple[Path, float]]] = {}
        for (year, month, day), path_mtimes in by_date.items():
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            csv_path = csv_base / f"{year:04d}" / f"{month:02d}" / f"{date_str}.csv"
            html_path = html_base / f"{year:04d}" / f"{month:02d}" / f"{date_str}.html"
            paths = [p for p, _ in path_mtimes]
            if not _day_in_stale_window(year, month, day):
                if csv_path.exists() and html_path.exists():
                    continue
            if not consolidated_outputs_fresher_than_focus_inputs(paths, csv_path, html_path):
                filtered[(year, month, day)] = path_mtimes
        by_date = filtered
        print(f"Update mode: {len(by_date)} day(s) with new/changed focus reports or missing consolidated output")

    # Track which months had at least one daily report updated (for monthly report logic)
    months_with_updated_days: set[tuple[int, int]] = set()

    for (year, month, day), path_mtimes in sorted(by_date.items()):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        csv_out_dir = csv_base / f"{year:04d}" / f"{month:02d}"
        html_out_dir = html_base / f"{year:04d}" / f"{month:02d}"
        csv_path = csv_out_dir / f"{date_str}.csv"
        html_path = html_out_dir / f"{date_str}.html"
        paths_for_fresh = [p for p, _ in path_mtimes]
        if not force and not _day_in_stale_window(year, month, day):
            if csv_path.exists() and html_path.exists():
                if not update:
                    print(
                        f"  {date_str}: skipped (outside daily stale-check window; use -force to rebuild)"
                    )
                continue
        if (
            not force
            and _day_in_stale_window(year, month, day)
            and consolidated_outputs_fresher_than_focus_inputs(paths_for_fresh, csv_path, html_path)
        ):
            if not update:
                print(
                    f"  {date_str}: skipped (consolidated outputs up to date vs focus inputs; use -force to rebuild)"
                )
            continue

        all_rows = []
        fieldnames = None
        paths = [p for p, _ in path_mtimes]

        for p in paths:
            try:
                rows = read_csv_rows(p, (year, month, day))
                if rows:
                    if fieldnames is None:
                        fieldnames = [k for k in rows[0].keys() if not k.startswith("_")]
                    all_rows.extend(rows)
            except Exception as e:
                print(f"Warning: could not read {p}: {e}")

        if not all_rows or not fieldnames:
            continue

        consolidated = consolidate_day(all_rows, fieldnames)
        if not consolidated:
            continue

        if anon_state:
            apply_anon_to_rows(consolidated, anon_state)

        csv_out_dir.mkdir(parents=True, exist_ok=True)
        html_out_dir.mkdir(parents=True, exist_ok=True)

        out_fieldnames = list(fieldnames) + [
            "OwnerCreatedBy",
            "OwnerCreatedDate",
            "OwnerCreatedAgeDays",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=out_fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(consolidated)

        generate_html_dashboard(consolidated, date_str, html_path)

        months_with_updated_days.add((year, month))
        print(f"  {date_str}: {len(consolidated)} resources from {len(paths)} file(s) -> {csv_path} + {html_path}")

    # Generate monthly consolidated reports per year/month folder
    # Only update when: monthly report doesn't exist, or month had updated daily records
    for month_dir in sorted(csv_base.glob("*/*")):
        if not month_dir.is_dir():
            continue
        parts = month_dir.relative_to(csv_base).parts
        if len(parts) != 2:
            continue
        try:
            year, month = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        month_str = f"{year:04d}-{month:02d}"
        month_html = html_base / f"{year:04d}" / f"{month_str}.html"
        month_csv = csv_base / f"{year:04d}" / f"{month_str}.csv"

        # Skip monthly update unless: monthly doesn't exist, or we updated days in this month (when -update)
        if update and not force:
            if month_html.exists() and (year, month) not in months_with_updated_days:
                print(f"  {month_str}: skipped (no updated daily records)")
                continue

        daily_csvs = sorted(month_dir.glob("????-??-??.csv"))
        if not daily_csvs:
            continue
        all_daily_rows = []
        fieldnames = None
        daily_rows_filter_data: list[dict] = []
        for csv_path in daily_csvs:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})\.csv$", csv_path.name)
            if not m:
                continue
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
                    rows = list(csv.DictReader(f))
                daily_rows_filter_data.append(
                    {
                        "date": f"{y:04d}-{mo:02d}-{d:02d}",
                        "items": [build_rows_filter_item(row) for row in rows],
                    }
                )
                for row in rows:
                    row["_ReportDate"] = (y, mo, d)
                    all_daily_rows.append(row)
                if anon_state:
                    apply_anon_to_rows(rows, anon_state)
                    for i, row in enumerate(rows):
                        items = daily_rows_filter_data[-1]["items"]
                        if i < len(items):
                            items[i] = build_rows_filter_item(row)
                if rows and fieldnames is None:
                    fieldnames = [k for k in rows[0].keys() if not k.startswith("_")]
            except Exception as e:
                print(f"Warning: could not read {csv_path}: {e}")
        if not all_daily_rows or not fieldnames:
            continue
        monthly = consolidate_month(all_daily_rows, fieldnames, year, month)
        if not monthly:
            continue
        month_csv.parent.mkdir(parents=True, exist_ok=True)
        month_html.parent.mkdir(parents=True, exist_ok=True)
        out_fieldnames = [k for k in monthly[0].keys() if not k.startswith("_")]
        with open(month_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=out_fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(monthly)
        generate_html_dashboard(
            monthly,
            month_str,
            month_html,
            daily_rows_filter_data=daily_rows_filter_data,
        )
        print(f"  {month_str}: monthly consolidated {len(monthly)} resources -> {month_csv} + {month_html}")

    # Generate HTML for any existing consolidated CSVs not processed above
    for csv_file in sorted(csv_base.glob("**/*.csv")):
        date_str = csv_file.stem
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            continue
        rel = csv_file.relative_to(csv_base)
        html_path = html_base / rel.with_suffix(".html")
        if html_path.exists() and html_path.stat().st_mtime >= csv_file.stat().st_mtime:
            continue
        try:
            with open(csv_file, newline="", encoding="utf-8", errors="replace") as f:
                rows = list(csv.DictReader(f))
            if rows:
                if anon_state:
                    apply_anon_to_rows(rows, anon_state)
                html_path.parent.mkdir(parents=True, exist_ok=True)
                generate_html_dashboard(rows, date_str, html_path)
                print(f"  {date_str}: generated {html_path} from existing CSV")
        except Exception as e:
            print(f"Warning: could not generate HTML for {csv_file}: {e}")

    if anon_state and anon_state["mapping"]:
        save_anon_mapping(output_root, anon_state)
        print(f"(anon: {len(anon_state['mapping'])} unique owner(s) anonymized, mapping saved to {_anon_mapping_path(output_root).name})")


def main():
    parser = argparse.ArgumentParser(description="Consolidate FOCUS reports into daily and monthly reports.")
    parser.add_argument(
        "-update",
        action="store_true",
        help="Only process days with newer focus reports; update monthly when daily changed",
    )
    parser.add_argument(
        "-force",
        action="store_true",
        help="Rebuild all daily/monthly consolidated reports (overrides -update and skip-if-exists)",
    )
    parser.add_argument(
        "-anon",
        action="store_true",
        help="Anonymize owner names: replace each unique user with anon##### (consistent across reports)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    base_dir = script_dir / "FocusReports" / "FOCUS Reports"
    output_dir = script_dir / "FocusReports"

    if not base_dir.exists():
        print(f"Focus reports directory not found: {base_dir}")
        return

    print("Consolidating FOCUS reports by ResourceID + SkuId...")
    if args.force:
        print("(force mode: updating all reports)")
    elif args.update:
        print("(update mode: only newer focus reports)")
    else:
        print("(skip days only when consolidated outputs are up to date vs focus inputs; use -force to rebuild all)")
    if args.anon:
        print("(anon mode: anonymizing owner names)")
    print("-" * 60)
    consolidate_reports(
        str(base_dir),
        str(output_dir),
        force=args.force,
        update=args.update,
        anon=args.anon,
    )
    print("-" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
