"""
Resolve missing ResourceName values from Focus reports via OCI Resource Search.

Reads unique resources from the last N days of FocusReports/ConsolidatedCSV.
When ResourceName is empty and ResourceId is an OCID, runs FreeTextSearch with
that ResourceId and uses display_name (or name) from the result.

If the resource is not found, or lifecycle_state is TERMINATED/DELETED, the
stored name gets " (deleted)" appended.

Output: relationships/oci-resource-names.json
  Plain object: { "<resourceId>": "<resourceName>", ... }
  Unresolved / not-found names fall back to ResourceId (with " (deleted)" when applicable).

Usage:
    python getResourceNames.py -day 30
    python getResourceNames.py -day 7 -rg eu-frankfurt-1,uk-london-1
"""

from __future__ import annotations

import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import oci

from consolidateReports import find_daily_reports_last_n_days, find_most_recent_daily_consolidated_csv
from ocimodules.functions import check_oci_version, signer_from_command_line, input_command_line, MyWriter
from ocimodules.IAM import Login, SubscribedRegions

# Disable OCI CircuitBreaker feature
oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

#################################################
#           Application Configuration           #
#################################################
min_version_required = "2.164.0"
application_version = "16.07.2026"

DEFAULT_DAY_WINDOW = 30
OUTPUT_JSON_NAME = "oci-resource-names.json"
MAX_REGION_WORKERS = 15
MAX_SEARCH_WORKERS = 10
DELETED_MARKER = " (deleted)"
_DELETED_LIFECYCLE_STATES = frozenset({"TERMINATED", "DELETED"})

_region_print_lock = threading.Lock()


def _region_print(*args, **kwargs) -> None:
    with _region_print_lock:
        print(*args, **kwargs)


def parse_day_window(raw) -> int:
    """Interpret -day as last N days (positive integer)."""
    if raw is None or str(raw).strip() == "":
        return DEFAULT_DAY_WINDOW
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"Invalid -day value {raw!r}; expected a positive integer.")
        sys.exit(1)
    if n < 1:
        print(f"Invalid -day value {n}; expected a positive integer.")
        sys.exit(1)
    return n


def is_ocid(value: str) -> bool:
    return value.startswith("ocid1.")


def region_from_ocid(ocid: str) -> str:
    """
    Extract region from OCID: ocid1.<type>.<realm>.<region>.<unique>.
    Returns empty string for global resources (empty region segment).
    """
    parts = ocid.split(".")
    if len(parts) >= 5 and parts[3]:
        return parts[3]
    return ""


def name_from_search_item(item) -> str:
    """Prefer display_name, then name, then additional_details.name."""
    display = (getattr(item, "display_name", None) or "").strip()
    if display:
        return display
    name = (getattr(item, "name", None) or "").strip()
    if name:
        return name
    additional = getattr(item, "additional_details", None) or {}
    if isinstance(additional, dict):
        for key in ("name", "display_name", "displayName"):
            val = additional.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def with_deleted_marker(name: str) -> str:
    """Append (deleted) once to a resource name."""
    name = (name or "").strip()
    if not name:
        return name
    if name.endswith(DELETED_MARKER) or name.endswith("(deleted)"):
        return name
    return f"{name}{DELETED_MARKER}"


def is_deleted_lifecycle(state: str | None) -> bool:
    return (state or "").strip().upper() in _DELETED_LIFECYCLE_STATES


def collect_unique_resources(
    consolidated_dir: Path,
    end_date_str: str,
    n_days: int,
    region_filter: set[str] | None = None,
) -> dict[str, dict]:
    """
    Scan last n_days of consolidated CSVs for unique ResourceId values.
    Returns resourceId -> {resourceId, resourceName, resourceType, region, compartmentId, source}.
    """
    resources: dict[str, dict] = {}
    daily = find_daily_reports_last_n_days(consolidated_dir, end_date_str, n_days)
    if not daily:
        return resources

    for csv_path, _date_str in daily:
        try:
            with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    rid = (row.get("ResourceId") or "").strip()
                    if not rid:
                        continue
                    region = (row.get("Region") or "").strip()
                    if not region and is_ocid(rid):
                        region = region_from_ocid(rid)
                    if region_filter and region and region not in region_filter:
                        continue
                    rname = (row.get("ResourceName") or "").strip()
                    rtype = (row.get("ResourceType") or "").strip()
                    compartment = (row.get("oci_CompartmentId") or "").strip()
                    existing = resources.get(rid)
                    if existing is None:
                        resources[rid] = {
                            "resourceId": rid,
                            "resourceName": rname,
                            "resourceType": rtype,
                            "region": region,
                            "compartmentId": compartment,
                            "source": "focus" if rname else "unresolved",
                        }
                    else:
                        if rname and not existing.get("resourceName"):
                            existing["resourceName"] = rname
                            existing["source"] = "focus"
                        if region and not existing.get("region"):
                            existing["region"] = region
                        if rtype and not existing.get("resourceType"):
                            existing["resourceType"] = rtype
                        if compartment and not existing.get("compartmentId"):
                            existing["compartmentId"] = compartment
        except OSError as e:
            print(f"  Warning: could not read {csv_path}: {e}")

    return resources


def search_resource_name(search_client, resource_id: str) -> tuple[str, bool]:
    """
    FreeTextSearch for resource_id.

    Returns (name, is_deleted):
      - found active: (display_name, False)
      - found terminated/deleted: (display_name, True)
      - not found: ("", True)
      - search/API error: ("", False)  # unknown; do not mark deleted
    """
    details = oci.resource_search.models.FreeTextSearchDetails(
        text=resource_id,
        matching_context_type=oci.resource_search.models.FreeTextSearchDetails.MATCHING_CONTEXT_TYPE_NONE,
    )
    try:
        response = search_client.search_resources(
            details,
            limit=10,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        )
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(f"  Search failed for {resource_id}: {e.status} {e.message}")
        return "", False
    except oci.exceptions.RequestException as e:
        _region_print(f"  Search connection error for {resource_id}: {e}")
        return "", False

    items = getattr(response, "data", None)
    items = getattr(items, "items", None) or []
    # Free-text can return unrelated hits; only accept exact OCID match.
    for item in items:
        identifier = (getattr(item, "identifier", None) or "").strip()
        if identifier == resource_id:
            name = name_from_search_item(item) or resource_id
            state = getattr(item, "lifecycle_state", None)
            return name, is_deleted_lifecycle(state)
    return "", True


def resolve_names_for_region(
    region: str,
    base_config: dict,
    signer,
    resources: list[dict],
) -> list[dict]:
    """Resolve empty ResourceName values for resources in one region via Resource Search."""
    if not resources:
        return []

    region_config = dict(base_config)
    region_config["region"] = region
    search_client = oci.resource_search.ResourceSearchClient(region_config, signer=signer)

    to_search: list[dict] = []
    resolved: list[dict] = []
    for r in resources:
        if (r.get("resourceName") or "").strip():
            resolved.append(dict(r))
        elif not is_ocid(r["resourceId"]):
            entry = dict(r)
            entry["resourceName"] = r["resourceId"]
            entry["source"] = "unresolved"
            resolved.append(entry)
        else:
            to_search.append(r)

    _region_print(
        f"\n--- Region: {region} "
        f"({len(resources)} resource(s), {len(to_search)} to search) ---"
    )
    if not to_search:
        return resolved

    try:
        with ThreadPoolExecutor(
            max_workers=min(len(to_search), MAX_SEARCH_WORKERS)
        ) as executor:
            futures = {
                executor.submit(search_resource_name, search_client, r["resourceId"]): r
                for r in to_search
            }
            for fut in as_completed(futures):
                src = futures[fut]
                entry = dict(src)
                try:
                    name, deleted = fut.result()
                except oci.exceptions.ServiceError as e:
                    if e.status == 401:
                        raise
                    _region_print(
                        f"  Search failed for {src['resourceId']}: {e.status} {e.message}"
                    )
                    name, deleted = "", False
                if name:
                    display = with_deleted_marker(name) if deleted else name
                    entry["resourceName"] = display
                    entry["source"] = "oci_search"
                    _region_print(f"  {src['resourceId']}: {display}")
                elif deleted:
                    display = with_deleted_marker(src["resourceId"])
                    entry["resourceName"] = display
                    entry["source"] = "unresolved"
                    _region_print(f"  {src['resourceId']}: {display}")
                else:
                    entry["resourceName"] = src["resourceId"]
                    entry["source"] = "unresolved"
                resolved.append(entry)
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            _region_print(
                f"  Skipping region {region}: unauthorized (401). {e.message}"
            )
            return [dict(r) for r in resources]
        raise
    except oci.exceptions.RequestException as e:
        _region_print(f"  Skipping region {region}: connection/DNS error. {e}")
        return [dict(r) for r in resources]

    return resolved


##########################################################################
# Main Program
##########################################################################

def main() -> None:
    print("OCI - OCVS Focus Reports — get resource names")
    print("============================================================================================")
    print("")

    cmd = input_command_line()

    if cmd.log_file != "":
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    n_days = parse_day_window(cmd.day)
    print(f"Resource-name window: last {n_days} day(s) of consolidated Focus reports")

    config, signer = signer_from_command_line(cmd)
    tenant_id = config["tenancy"]
    Login(config, signer, tenant_id, get_compartments=False)
    print(f"Current configured region is: {config['region']}")

    regions = SubscribedRegions(config, signer)
    regions = (
        [r.strip() for r in cmd.regions.split(",") if r.strip()] if cmd.regions else regions
    )
    region_filter = set(regions) if cmd.regions else None
    print("Regions to process: {}".format(regions))

    check_oci_version(min_version_required)

    script_dir = Path(__file__).resolve().parent
    consolidated_csv_dir = script_dir / "FocusReports" / "ConsolidatedCSV"
    relationships_dir = script_dir / "relationships"
    relationships_dir.mkdir(parents=True, exist_ok=True)
    out_json = relationships_dir / OUTPUT_JSON_NAME

    recent = find_most_recent_daily_consolidated_csv(consolidated_csv_dir)
    if not recent:
        # Not an error: on a first run the consolidated reports do not exist yet.
        print(f"No daily consolidated CSV under {consolidated_csv_dir}; nothing to do "
              "(run makeDailyReports.py first).")
        sys.exit(0)

    _, end_date_str = recent
    print(f"Most recent consolidated day: {end_date_str}")
    print("Scanning ConsolidatedCSV for unique resources...")

    resources_by_id = collect_unique_resources(
        consolidated_csv_dir, end_date_str, n_days, region_filter=region_filter
    )
    print(f"Unique resources found: {len(resources_by_id)}")

    named_focus = sum(1 for r in resources_by_id.values() if r.get("source") == "focus")
    need_search = sum(
        1
        for r in resources_by_id.values()
        if r.get("source") != "focus" and is_ocid(r["resourceId"])
    )
    print(f"Already named in Focus: {named_focus}")
    print(f"Empty ResourceName (OCI search candidates): {need_search}")

    by_region: dict[str, list[dict]] = {}
    resolved_resources: list[dict] = []
    no_region = 0
    for res in resources_by_id.values():
        # Already named in Focus — keep as-is, no search.
        if (res.get("resourceName") or "").strip():
            resolved_resources.append(dict(res))
            continue
        region = (res.get("region") or "").strip()
        if not region:
            no_region += 1
            entry = dict(res)
            entry["resourceName"] = res["resourceId"]
            entry["source"] = "unresolved"
            resolved_resources.append(entry)
            continue
        if region_filter and region not in region_filter:
            continue
        by_region.setdefault(region, []).append(res)

    if no_region:
        print(
            f"Warning: {no_region} resource(s) missing Region; "
            "using ResourceId as name."
        )

    active_regions = [r for r in regions if r in by_region]
    # Also process Focus regions present in data but not listed above
    for region in sorted(by_region.keys()):
        if region not in active_regions:
            active_regions.append(region)

    if active_regions:
        with ThreadPoolExecutor(
            max_workers=min(len(active_regions), MAX_REGION_WORKERS)
        ) as executor:
            futures = [
                executor.submit(
                    resolve_names_for_region,
                    region,
                    config,
                    signer,
                    by_region[region],
                )
                for region in active_regions
            ]
            for fut in futures:
                resolved_resources.extend(fut.result())

    # Deduplicate by resourceId (prefer named / oci_search over unresolved)
    by_id: dict[str, dict] = {}
    source_rank = {"focus": 3, "oci_search": 2, "unresolved": 1}
    for item in resolved_resources:
        rid = item["resourceId"]
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = item
            continue
        if source_rank.get(item.get("source"), 0) > source_rank.get(existing.get("source"), 0):
            by_id[rid] = item
        elif (
            source_rank.get(item.get("source"), 0) == source_rank.get(existing.get("source"), 0)
            and (item.get("resourceName") or "")
            and not (existing.get("resourceName") or "")
        ):
            by_id[rid] = item

    resource_names = {
        rid: (item.get("resourceName") or "").strip() or rid
        for rid, item in sorted(by_id.items(), key=lambda kv: kv[0])
    }
    search_resolved = sum(1 for r in by_id.values() if r.get("source") == "oci_search")
    focus_named = sum(1 for r in by_id.values() if r.get("source") == "focus")

    out_json.write_text(json.dumps(resource_names, indent=2), encoding="utf-8")
    print(
        f"\nWrote {len(resource_names)} resource name(s) to {out_json} "
        f"({focus_named} from Focus, {search_resolved} from OCI search, "
        f"{len(resource_names) - focus_named - search_resolved} fallback to ResourceId)"
    )


if __name__ == "__main__":
    main()
