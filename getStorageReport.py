"""
Fetch unattached block and boot volumes from OCI and write raw rows to disk for makeStorageReport.py.

Enriches each row with billed_cost_last_30_days from FocusReports/ConsolidatedCSV (sum of BilledCost
over the last 30 daily files ending at the most recent consolidated report), when that folder exists.

When config.json has SetDeletionForUnattachedDisk true, applies defined tags (namespace and keys from
unattachedVolumeDeletionTags) to each unattached block/boot volume via the Block Storage API.

Output: consolidateReports/StorageReport/unattached_volumes.json
"""

import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import oci

from ocimodules.functions import input_command_line, signer_from_command_line, check_oci_version, MyWriter
from ocimodules.IAM import Login, SubscribedRegions, GetCompartmentFullPath
from consolidateReports import (
    age_in_days,
    find_most_recent_daily_consolidated_csv,
    format_created_date_display,
    get_tag_config,
    get_unattached_volume_deletion_tag_config,
    sum_billed_cost_by_resource_ids_last_n_days,
)

# Disable OCI CircuitBreaker feature
oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

#################################################
#           Application Configuration           #
#################################################
min_version_required = "2.164.0"
application_version = "11.03.2026"

STORAGE_JSON_NAME = "unattached_volumes.json"
SCHEMA_VERSION = 1
# Cap concurrent region queries to reduce DNS/socket pressure (Windows getaddrinfo failures under load).
MAX_REGION_WORKERS = 15

_region_print_lock = threading.Lock()


def _region_print(*args, **kwargs) -> None:
    with _region_print_lock:
        print(*args, **kwargs)


def get_defined_tag_value(defined_tags: dict | None, namespace_dot_key: str) -> str:
    """
    Read a value from OCI defined_tags: { "Namespace": { "TagKey": "value" } }.
    namespace_dot_key is e.g. Oracle-Tags.CreatedBy (namespace Oracle-Tags, key CreatedBy).
    """
    if not defined_tags or not namespace_dot_key or "." not in namespace_dot_key:
        return ""
    ns, tag_key = namespace_dot_key.split(".", 1)
    bucket = defined_tags.get(ns)
    if isinstance(bucket, dict):
        val = bucket.get(tag_key)
        if val is not None:
            return str(val).strip()
    return ""


def attached_volume_ocids(compute_client, compartment_id):
    ids = set()
    for va in oci.pagination.list_call_get_all_results(
        compute_client.list_volume_attachments,
        compartment_id=compartment_id,
        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
    ).data:
        vid = getattr(va, "volume_id", None)
        if vid:
            ids.add(vid)
    return ids


def list_region_availability_domain_names(identity_client, compartment_id: str) -> list[str]:
    """Availability domain names for the region (tenant compartment lists all ADs in that region)."""
    try:
        resp = identity_client.list_availability_domains(compartment_id=compartment_id)
        return [ad.name for ad in resp.data]
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(f"  List availability domains failed: {e.status} {e.message}")
        return []


def attached_boot_volume_ocids(
    compute_client,
    compartment_id: str,
    availability_domains: list[str],
) -> set[str]:
    """
    Boot volumes attached to compute instances use list_boot_volume_attachments (/bootVolumeAttachments),
    not list_volume_attachments — the latter only returns iSCSI/PV *block* volume attachments.
    """
    ids: set[str] = set()
    in_use = {"ATTACHED", "ATTACHING"}
    for ad_name in availability_domains:
        try:
            for bva in oci.pagination.list_call_get_all_results(
                compute_client.list_boot_volume_attachments,
                availability_domain=ad_name,
                compartment_id=compartment_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data:
                st = getattr(bva, "lifecycle_state", None) or ""
                if st not in in_use:
                    continue
                bid = getattr(bva, "boot_volume_id", None)
                if bid:
                    ids.add(bid)
        except oci.exceptions.ServiceError as e:
            if e.status == 401:
                raise
            _region_print(
                f"  Skip boot volume attachments AD={ad_name} compartment={compartment_id}: {e.status} {e.message}"
            )
    return ids


def _merge_deletion_into_defined_tags(
    existing,
    namespace: str,
    schedule_key: str,
    next_key: str,
    schedule_value,
    next_date_iso: str,
) -> dict:
    """Full defined_tags map for update_volume / update_boot_volume (existing tags preserved)."""
    base = copy.deepcopy(existing) if existing else {}
    if not isinstance(base, dict):
        base = {}
    ns = base.get(namespace)
    if not isinstance(ns, dict):
        ns = {}
    else:
        ns = dict(ns)
    ns[schedule_key] = schedule_value
    ns[next_key] = next_date_iso
    base[namespace] = ns
    return base


def _apply_deletion_tags_to_unattached_volume(
    block_client,
    vol,
    kind: str,
    namespace: str,
    schedule_key: str,
    next_key: str,
    schedule_value,
    next_date_iso: str,
) -> None:
    existing = getattr(vol, "defined_tags", None) or {}
    if not isinstance(existing, dict):
        existing = {}
    merged = _merge_deletion_into_defined_tags(
        existing, namespace, schedule_key, next_key, schedule_value, next_date_iso
    )
    if kind == "boot":
        details = oci.core.models.UpdateBootVolumeDetails(defined_tags=merged)
        block_client.update_boot_volume(vol.id, details)
    else:
        details = oci.core.models.UpdateVolumeDetails(defined_tags=merged)
        block_client.update_volume(vol.id, details)


def build_storage_row_dict(
    vol,
    region: str,
    cid: str,
    compartments,
    kind: str,
    created_by_key: str,
    created_date_key: str,
) -> dict:
    """Build one dashboard row from a Volume or BootVolume API object."""
    dtags = getattr(vol, "defined_tags", None) or {}
    if not isinstance(dtags, dict):
        dtags = {}
    owner = get_defined_tag_value(dtags, created_by_key)
    created_raw = get_defined_tag_value(dtags, created_date_key)
    created_display = format_created_date_display(created_raw) if created_raw else ""
    age_days = age_in_days(created_raw)
    return {
        "region": region,
        "compartment_path": GetCompartmentFullPath(compartments, cid) or cid,
        "volume_kind": kind,
        "display_name": getattr(vol, "display_name", "") or "",
        "ocid": vol.id,
        "lifecycle_state": getattr(vol, "lifecycle_state", None) or "",
        "size_in_gbs": getattr(vol, "size_in_gbs", "") or "",
        "owner": owner,
        "created_display": created_display,
        "age_days": age_days,
    }


def gather_unattached_rows_for_region(
    region: str,
    base_config: dict,
    signer,
    tenant_id: str,
    compartments,
    created_by_key: str,
    created_date_key: str,
    set_deletion_tags: bool,
    deletion_namespace: str,
    schedule_key: str,
    next_schedule_key: str,
    schedule_value,
    next_schedule_date_iso: str,
) -> list[dict]:
    """List unattached block/boot volumes for one region (for parallel execution per region)."""
    region_config = dict(base_config)
    region_config["region"] = region
    block_client = oci.core.BlockstorageClient(region_config, signer=signer)
    compute_client = oci.core.ComputeClient(region_config, signer=signer)
    identity_client = oci.identity.IdentityClient(region_config, signer=signer)

    _region_print(f"\n--- Region: {region} ---")

    try:
        return _gather_unattached_rows_for_region_body(
            region,
            block_client,
            compute_client,
            identity_client,
            tenant_id,
            compartments,
            created_by_key,
            created_date_key,
            set_deletion_tags,
            deletion_namespace,
            schedule_key,
            next_schedule_key,
            schedule_value,
            next_schedule_date_iso,
        )
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            _region_print(
                f"  Skipping entire region (storage report): unauthorized (401). {e.message}"
            )
            return []
        raise
    except oci.exceptions.RequestException as e:
        # DNS / TLS / connection pool failures (e.g. Windows getaddrinfo 11001) are not ServiceError.
        _region_print(
            f"  Skipping entire region (storage report): connection or DNS error. {e}"
        )
        return []


def _gather_unattached_rows_for_region_body(
    region: str,
    block_client,
    compute_client,
    identity_client,
    tenant_id: str,
    compartments,
    created_by_key: str,
    created_date_key: str,
    set_deletion_tags: bool,
    deletion_namespace: str,
    schedule_key: str,
    next_schedule_key: str,
    schedule_value,
    next_schedule_date_iso: str,
) -> list[dict]:
    ad_names = list_region_availability_domain_names(identity_client, tenant_id)
    if not ad_names:
        _region_print(
            "  Warning: no availability domains; boot volumes in use by instances may be misclassified."
        )

    attached_in_region = set()
    for comp in compartments:
        cid = comp.details.id
        try:
            attached_in_region.update(attached_volume_ocids(compute_client, cid))
        except oci.exceptions.ServiceError as e:
            if e.status == 401:
                raise
            _region_print(
                f"  Skip block volume attachment scan for compartment {cid}: {e.status} {e.message}"
            )
        attached_in_region.update(attached_boot_volume_ocids(compute_client, cid, ad_names))

    rows: list[dict] = []
    region_count = 0
    for comp in compartments:
        cid = comp.details.id
        seen_ocids: set[str] = set()
        try:
            for vol in oci.pagination.list_call_get_all_results(
                block_client.list_volumes,
                compartment_id=cid,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data:
                st = getattr(vol, "lifecycle_state", None) or ""
                if st in ("TERMINATED", "TERMINATING"):
                    continue
                if vol.id in attached_in_region or vol.id in seen_ocids:
                    continue
                seen_ocids.add(vol.id)
                kind = "boot" if getattr(vol, "is_boot_volume", False) else "block"
                rows.append(
                    build_storage_row_dict(
                        vol, region, cid, compartments, kind, created_by_key, created_date_key
                    )
                )
                if set_deletion_tags:
                    try:
                        _apply_deletion_tags_to_unattached_volume(
                            block_client,
                            vol,
                            kind,
                            deletion_namespace,
                            schedule_key,
                            next_schedule_key,
                            schedule_value,
                            next_schedule_date_iso,
                        )
                    except oci.exceptions.ServiceError as e:
                        if e.status == 401:
                            raise
                        _region_print(
                            f"  Could not set deletion tags on {kind} volume {vol.id}: "
                            f"{e.status} {e.message}"
                        )
                region_count += 1
        except oci.exceptions.ServiceError as e:
            if e.status == 401:
                raise
            _region_print(f"  Skip block volume list for compartment {cid}: {e.status} {e.message}")
        try:
            for vol in oci.pagination.list_call_get_all_results(
                block_client.list_boot_volumes,
                compartment_id=cid,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data:
                st = getattr(vol, "lifecycle_state", None) or ""
                if st in ("TERMINATED", "TERMINATING"):
                    continue
                if vol.id in attached_in_region or vol.id in seen_ocids:
                    continue
                seen_ocids.add(vol.id)
                rows.append(
                    build_storage_row_dict(
                        vol, region, cid, compartments, "boot", created_by_key, created_date_key
                    )
                )
                if set_deletion_tags:
                    try:
                        _apply_deletion_tags_to_unattached_volume(
                            block_client,
                            vol,
                            "boot",
                            deletion_namespace,
                            schedule_key,
                            next_schedule_key,
                            schedule_value,
                            next_schedule_date_iso,
                        )
                    except oci.exceptions.ServiceError as e:
                        if e.status == 401:
                            raise
                        _region_print(
                            f"  Could not set deletion tags on boot volume {vol.id}: "
                            f"{e.status} {e.message}"
                        )
                region_count += 1
        except oci.exceptions.ServiceError as e:
            if e.status == 401:
                raise
            _region_print(f"  Skip boot volume list for compartment {cid}: {e.status} {e.message}")

    _region_print(f"  Unattached block + boot volumes in {region}: {region_count}")
    return rows


##########################################################################
# Main Program
##########################################################################

print("OCI - OCVS Focus Reports — get storage (unattached volumes)")
print("============================================================================================")
print("")

cmd = input_command_line()

if cmd.log_file != "":
    writer = MyWriter(sys.stdout, cmd.log_file)
    sys.stdout = writer

config, signer = signer_from_command_line(cmd)
tenant_id = config["tenancy"]
compartments = Login(config, signer, tenant_id, get_compartments=True)
print(f"Current configured region is: {config['region']}")

regions = SubscribedRegions(config, signer)
regions = cmd.regions.split(",") if cmd.regions else regions
print("Regions to process: {}".format(regions))

check_oci_version(min_version_required)

generated_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
script_dir = Path(__file__).resolve().parent
storage_dir = script_dir / "consolidateReports" / "StorageReport"
storage_dir.mkdir(parents=True, exist_ok=True)
out_json = storage_dir / STORAGE_JSON_NAME

created_by_key, created_date_key = get_tag_config()
(
    set_deletion_tags,
    deletion_namespace,
    del_sched_key,
    del_next_key,
    del_days_ahead,
    del_schedule_value,
) = get_unattached_volume_deletion_tag_config()
next_schedule_date_iso = (date.today() + timedelta(days=del_days_ahead)).isoformat()
if set_deletion_tags:
    print(
        "SetDeletionForUnattachedDisk: applying defined tags "
        f"({deletion_namespace}.{del_sched_key}, {deletion_namespace}.{del_next_key}={next_schedule_date_iso}) "
        "to unattached block and boot volumes."
    )

rows: list[dict] = []
if regions:
    with ThreadPoolExecutor(max_workers=min(len(regions), MAX_REGION_WORKERS)) as executor:
        futures = [
            executor.submit(
                gather_unattached_rows_for_region,
                region,
                config,
                signer,
                tenant_id,
                compartments,
                created_by_key,
                created_date_key,
                set_deletion_tags,
                deletion_namespace,
                del_sched_key,
                del_next_key,
                del_schedule_value,
                next_schedule_date_iso,
            )
            for region in regions
        ]
        for fut in futures:
            rows.extend(fut.result())

consolidated_csv_dir = script_dir / "FocusReports" / "ConsolidatedCSV"
cost_basis_end_date: str | None = None
cost_by_ocid: dict[str, float] = {}
recent_csv = find_most_recent_daily_consolidated_csv(consolidated_csv_dir)
if recent_csv:
    _, cost_basis_end_date = recent_csv
    ocid_set = {(r.get("ocid") or "").strip() for r in rows if (r.get("ocid") or "").strip()}
    cost_by_ocid = sum_billed_cost_by_resource_ids_last_n_days(
        consolidated_csv_dir, cost_basis_end_date, ocid_set, 30
    )
    print(
        f"\nConsolidated CSV: cost window last 30 day(s) ending {cost_basis_end_date} "
        f"({consolidated_csv_dir})"
    )
else:
    print(f"\nNo daily consolidated CSV under {consolidated_csv_dir}; billed_cost_last_30_days set to null.")

for r in rows:
    ocid = (r.get("ocid") or "").strip()
    if cost_basis_end_date and ocid:
        raw = cost_by_ocid.get(ocid, 0.0)
        r["billed_cost_last_30_days"] = round(raw, 6)
    else:
        r["billed_cost_last_30_days"] = None

payload = {
    "schema_version": SCHEMA_VERSION,
    "application_version": application_version,
    "generated_at": datetime.now().isoformat(),
    "generated_label": generated_label,
    "cost_basis_end_date": cost_basis_end_date,
    "cost_window_days": 30 if cost_basis_end_date else None,
    "row_count": len(rows),
    "rows": rows,
}
out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"\nWrote {len(rows)} row(s) to {out_json}")
