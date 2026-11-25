"""
Build resource master → related relationships from Focus reports + OCI APIs.

1) Compute instances (ResourceType == instance): master = instance; related =
   attached boot/block volumes and their backups.
2) OCVS ESXi hosts (ResourceType == vmwareesxihost): master = OCVS SDDC Cluster
   (from EsxiHostClient.get_esxi_host → cluster_id); related = ESXi host(s).
   The cluster has no FOCUS cost; consolidated reports use charge description
   "OCVS SDDC Cluster".

Output: relationships/oci-resource-relations.json

Usage:
    python getRelationships.py -day 30
    python getRelationships.py -day 7 -rg eu-frankfurt-1,uk-london-1
"""

from __future__ import annotations

import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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

SCHEMA_VERSION = 1
DEFAULT_DAY_WINDOW = 30
OUTPUT_JSON_NAME = "oci-resource-relations.json"
INSTANCE_RESOURCE_TYPE = "instance"
ESXI_HOST_RESOURCE_TYPE = "vmwareesxihost"
SDDC_CLUSTER_RESOURCE_TYPE = "vmwarecluster"
OCVS_SDDC_CLUSTER_CHARGE_DESCRIPTION = "OCVS SDDC Cluster"
BOOT_VOLUME_RESOURCE_TYPE = "bootvolume"
BLOCK_VOLUME_RESOURCE_TYPE = "volume"
BOOT_VOLUME_BACKUP_RESOURCE_TYPE = "bootvolumebackup"
BLOCK_VOLUME_BACKUP_RESOURCE_TYPE = "volumebackup"
MAX_REGION_WORKERS = 15
_BACKUP_ACTIVE_STATES = frozenset(
    {"AVAILABLE", "CREATING", "REQUEST_RECEIVED", "COPYING", "RESTORING"}
)

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


def collect_unique_resources_by_type(
    consolidated_dir: Path,
    end_date_str: str,
    n_days: int,
    resource_type: str,
    region_filter: set[str] | None = None,
) -> dict[str, dict]:
    """
    Scan last n_days of consolidated CSVs for a ResourceType.
    Unique key is ResourceId (FOCUS ResourceName is often empty).
    Returns resourceId -> {resourceId, resourceName, resourceType, region, compartmentId}.
    """
    want = (resource_type or "").strip().lower()
    found: dict[str, dict] = {}
    daily = find_daily_reports_last_n_days(consolidated_dir, end_date_str, n_days)
    if not daily:
        return found

    for csv_path, _date_str in daily:
        try:
            with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    rtype = (row.get("ResourceType") or "").strip().lower()
                    if rtype != want:
                        continue
                    rid = (row.get("ResourceId") or "").strip()
                    if not rid:
                        continue
                    region = (row.get("Region") or "").strip()
                    if region_filter and region and region not in region_filter:
                        continue
                    rname = (row.get("ResourceName") or "").strip()
                    compartment = (row.get("oci_CompartmentId") or "").strip()
                    existing = found.get(rid)
                    if existing is None:
                        found[rid] = {
                            "resourceId": rid,
                            "resourceName": rname,
                            "resourceType": want,
                            "region": region,
                            "compartmentId": compartment,
                        }
                    else:
                        if rname and not existing.get("resourceName"):
                            existing["resourceName"] = rname
                        if region and not existing.get("region"):
                            existing["region"] = region
                        if compartment and not existing.get("compartmentId"):
                            existing["compartmentId"] = compartment
        except OSError as e:
            print(f"  Warning: could not read {csv_path}: {e}")

    return found


def collect_unique_instances(
    consolidated_dir: Path,
    end_date_str: str,
    n_days: int,
    region_filter: set[str] | None = None,
) -> dict[str, dict]:
    """Scan consolidated CSVs for ResourceType == instance."""
    return collect_unique_resources_by_type(
        consolidated_dir,
        end_date_str,
        n_days,
        INSTANCE_RESOURCE_TYPE,
        region_filter=region_filter,
    )


def collect_unique_esxi_hosts(
    consolidated_dir: Path,
    end_date_str: str,
    n_days: int,
    region_filter: set[str] | None = None,
) -> dict[str, dict]:
    """Scan consolidated CSVs for ResourceType == vmwareesxihost."""
    return collect_unique_resources_by_type(
        consolidated_dir,
        end_date_str,
        n_days,
        ESXI_HOST_RESOURCE_TYPE,
        region_filter=region_filter,
    )


def _get_volume(block_client, volume_id: str, kind: str):
    """Fetch boot/block volume; return API object or None."""
    try:
        if kind == "boot":
            return block_client.get_boot_volume(volume_id).data
        return block_client.get_volume(volume_id).data
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(f"  Could not get {kind} volume {volume_id}: {e.status} {e.message}")
        return None


def _volume_has_backup_policy(block_client, asset_id: str) -> bool:
    """True if a volume backup policy is assigned (scheduled backups enabled)."""
    try:
        assignments = block_client.get_volume_backup_policy_asset_assignment(
            asset_id=asset_id,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        ).data
        return bool(assignments)
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(
            f"  Could not check backup policy for {asset_id}: {e.status} {e.message}"
        )
        return False


def _list_backups_for_volume(
    block_client,
    volume_id: str,
    compartment_id: str,
    kind: str,
) -> list[dict]:
    """
    List active boot/block volume backups for a volume.
    kind is 'boot' or 'block'. Returns related-object dicts.
    """
    if not compartment_id:
        return []

    backup_type = (
        BOOT_VOLUME_BACKUP_RESOURCE_TYPE if kind == "boot" else BLOCK_VOLUME_BACKUP_RESOURCE_TYPE
    )
    results: list[dict] = []
    try:
        if kind == "boot":
            backups = oci.pagination.list_call_get_all_results(
                block_client.list_boot_volume_backups,
                compartment_id,
                boot_volume_id=volume_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
        else:
            backups = oci.pagination.list_call_get_all_results(
                block_client.list_volume_backups,
                compartment_id,
                volume_id=volume_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(
            f"  Skip {kind} volume backups for {volume_id}: {e.status} {e.message}"
        )
        return []

    for bak in backups:
        st = (getattr(bak, "lifecycle_state", None) or "").upper()
        if st and st not in _BACKUP_ACTIVE_STATES:
            continue
        bak_id = getattr(bak, "id", None)
        if not bak_id:
            continue
        name = (getattr(bak, "display_name", None) or "").strip() or bak_id
        results.append(
            {
                "resourceName": name,
                "resourceType": backup_type,
                "resourceId": bak_id,
            }
        )
    return results


def related_resources_for_instance(
    compute_client,
    block_client,
    instance_id: str,
    compartment_id: str,
    availability_domain: str,
) -> list[dict]:
    """Return related boot/block volumes and their backups for one instance."""
    related: list[dict] = []
    seen: set[str] = set()
    in_use = {"ATTACHED", "ATTACHING"}

    def _add_volume_and_backups(volume_id: str, kind: str) -> None:
        if not volume_id or volume_id in seen:
            return
        seen.add(volume_id)
        vol = _get_volume(block_client, volume_id, kind)
        vol_name = volume_id
        vol_compartment = compartment_id
        if vol is not None:
            vol_name = (getattr(vol, "display_name", None) or "").strip() or volume_id
            vol_compartment = (
                getattr(vol, "compartment_id", None) or ""
            ).strip() or compartment_id

        related.append(
            {
                "resourceName": vol_name,
                "resourceType": (
                    BOOT_VOLUME_RESOURCE_TYPE if kind == "boot" else BLOCK_VOLUME_RESOURCE_TYPE
                ),
                "resourceId": volume_id,
            }
        )

        # Include backups when a backup policy is assigned and/or backup objects exist.
        has_policy = _volume_has_backup_policy(block_client, volume_id)
        backups = _list_backups_for_volume(
            block_client, volume_id, vol_compartment, kind
        )
        if not has_policy and not backups:
            return
        for bak in backups:
            bak_id = bak.get("resourceId")
            if bak_id and bak_id not in seen:
                seen.add(bak_id)
                related.append(bak)

    # Block volumes (iSCSI / paravirtualized attachments)
    try:
        for va in oci.pagination.list_call_get_all_results(
            compute_client.list_volume_attachments,
            compartment_id=compartment_id,
            instance_id=instance_id,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
        ).data:
            st = (getattr(va, "lifecycle_state", None) or "").upper()
            if st and st not in in_use:
                continue
            _add_volume_and_backups(getattr(va, "volume_id", None), "block")
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            raise
        _region_print(
            f"  Skip block attachments for {instance_id}: {e.status} {e.message}"
        )

    # Boot volume attachments require availability domain
    if availability_domain:
        try:
            for bva in oci.pagination.list_call_get_all_results(
                compute_client.list_boot_volume_attachments,
                availability_domain=availability_domain,
                compartment_id=compartment_id,
                instance_id=instance_id,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data:
                st = (getattr(bva, "lifecycle_state", None) or "").upper()
                if st and st not in in_use:
                    continue
                _add_volume_and_backups(getattr(bva, "boot_volume_id", None), "boot")
        except oci.exceptions.ServiceError as e:
            if e.status == 401:
                raise
            _region_print(
                f"  Skip boot attachments for {instance_id}: {e.status} {e.message}"
            )

    return related


def build_relationships_for_region(
    region: str,
    base_config: dict,
    signer,
    instances: list[dict],
) -> list[dict]:
    """Resolve OCI attachments and backups for all instances in one region."""
    if not instances:
        return []

    region_config = dict(base_config)
    region_config["region"] = region
    compute_client = oci.core.ComputeClient(region_config, signer=signer)
    block_client = oci.core.BlockstorageClient(region_config, signer=signer)

    _region_print(f"\n--- Region: {region} ({len(instances)} instance(s)) ---")
    results: list[dict] = []

    try:
        for inst in instances:
            instance_id = inst["resourceId"]
            compartment_id = (inst.get("compartmentId") or "").strip()
            resource_name = (inst.get("resourceName") or "").strip()
            availability_domain = ""

            try:
                api_inst = compute_client.get_instance(instance_id).data
                display = (getattr(api_inst, "display_name", None) or "").strip()
                if display:
                    resource_name = display
                if not compartment_id:
                    compartment_id = (getattr(api_inst, "compartment_id", None) or "").strip()
                availability_domain = (getattr(api_inst, "availability_domain", None) or "").strip()
            except oci.exceptions.ServiceError as e:
                if e.status == 401:
                    raise
                if e.status == 404:
                    _region_print(f"  Instance not found (skip): {instance_id}")
                    continue
                _region_print(
                    f"  get_instance failed for {instance_id}: {e.status} {e.message}"
                )
                if not compartment_id:
                    continue

            if not resource_name:
                resource_name = instance_id
            if not compartment_id:
                _region_print(f"  No compartment for {instance_id}; skip attachments")
                related: list[dict] = []
            else:
                related = related_resources_for_instance(
                    compute_client,
                    block_client,
                    instance_id,
                    compartment_id,
                    availability_domain,
                )

            results.append(
                {
                    "master": {
                        "resourceName": resource_name,
                        "resourceType": INSTANCE_RESOURCE_TYPE,
                        "resourceId": instance_id,
                    },
                    "related": related,
                }
            )
            backup_n = sum(
                1
                for r in related
                if r.get("resourceType")
                in (BOOT_VOLUME_BACKUP_RESOURCE_TYPE, BLOCK_VOLUME_BACKUP_RESOURCE_TYPE)
            )
            _region_print(
                f"  {resource_name}: {len(related)} related "
                f"({backup_n} backup(s))"
            )
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            _region_print(
                f"  Skipping region {region}: unauthorized (401). {e.message}"
            )
            return []
        raise
    except oci.exceptions.RequestException as e:
        _region_print(f"  Skipping region {region}: connection/DNS error. {e}")
        return []

    return results


def build_esxi_cluster_relationships_for_region(
    region: str,
    base_config: dict,
    signer,
    esxi_hosts: list[dict],
) -> list[dict]:
    """
    For each vmwareesxihost, resolve its OCVS SDDC Cluster via get_esxi_host.
    Master = cluster (no FOCUS cost); related = ESXi host(s) in that cluster.
    """
    if not esxi_hosts:
        return []

    region_config = dict(base_config)
    region_config["region"] = region
    esxi_client = oci.ocvp.EsxiHostClient(region_config, signer=signer)
    cluster_client = oci.ocvp.ClusterClient(region_config, signer=signer)

    _region_print(f"\n--- Region: {region} ({len(esxi_hosts)} ESXi host(s)) ---")
    by_cluster: dict[str, dict] = {}

    try:
        for host in esxi_hosts:
            host_id = host["resourceId"]
            host_name = (host.get("resourceName") or "").strip() or host_id
            try:
                esxi = esxi_client.get_esxi_host(host_id).data
            except oci.exceptions.ServiceError as e:
                if e.status == 401:
                    raise
                if e.status == 404:
                    _region_print(f"  ESXi host not found (skip): {host_id}")
                    continue
                _region_print(
                    f"  get_esxi_host failed for {host_id}: {e.status} {e.message}"
                )
                continue

            display = (getattr(esxi, "display_name", None) or "").strip()
            if display:
                host_name = display
            cluster_id = (getattr(esxi, "cluster_id", None) or "").strip()
            if not cluster_id:
                _region_print(f"  No cluster_id for ESXi host {host_name}; skip")
                continue

            entry = by_cluster.get(cluster_id)
            if entry is None:
                cluster_name = cluster_id
                try:
                    cluster = cluster_client.get_cluster(cluster_id).data
                    cname = (getattr(cluster, "display_name", None) or "").strip()
                    if cname:
                        cluster_name = cname
                except oci.exceptions.ServiceError as e:
                    if e.status == 401:
                        raise
                    _region_print(
                        f"  get_cluster failed for {cluster_id}: {e.status} {e.message}"
                    )
                entry = {
                    "master": {
                        "resourceName": cluster_name,
                        "resourceType": SDDC_CLUSTER_RESOURCE_TYPE,
                        "resourceId": cluster_id,
                        "chargeDescription": OCVS_SDDC_CLUSTER_CHARGE_DESCRIPTION,
                    },
                    "related": [],
                }
                by_cluster[cluster_id] = entry

            related = entry["related"]
            if not any(r.get("resourceId") == host_id for r in related):
                related.append(
                    {
                        "resourceName": host_name,
                        "resourceType": ESXI_HOST_RESOURCE_TYPE,
                        "resourceId": host_id,
                    }
                )
            _region_print(
                f"  {host_name} → cluster {(entry['master'].get('resourceName') or cluster_id)}"
            )
    except oci.exceptions.ServiceError as e:
        if e.status == 401:
            _region_print(
                f"  Skipping region {region}: unauthorized (401). {e.message}"
            )
            return []
        raise
    except oci.exceptions.RequestException as e:
        _region_print(f"  Skipping region {region}: connection/DNS error. {e}")
        return []

    return list(by_cluster.values())


def _group_resources_by_region(
    resources: dict[str, dict],
    region_filter: set[str] | None,
    label: str,
) -> dict[str, list[dict]]:
    by_region: dict[str, list[dict]] = {}
    no_region = 0
    for item in resources.values():
        region = (item.get("region") or "").strip()
        if not region:
            no_region += 1
            continue
        if region_filter and region not in region_filter:
            continue
        by_region.setdefault(region, []).append(item)
    if no_region:
        print(
            f"Warning: {no_region} {label} missing Region in Focus data; skipped."
        )
    return by_region


##########################################################################
# Main Program
##########################################################################

def main() -> None:
    print("OCI - OCVS Focus Reports — get resource relationships")
    print("============================================================================================")
    print("")

    cmd = input_command_line()

    if cmd.log_file != "":
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    n_days = parse_day_window(cmd.day)
    print(f"Relationship window: last {n_days} day(s) of consolidated Focus reports")

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

    print("Scanning ConsolidatedCSV for instances...")
    instances_by_id = collect_unique_instances(
        consolidated_csv_dir, end_date_str, n_days, region_filter=region_filter
    )
    print(f"Unique instances found: {len(instances_by_id)}")

    print("Scanning ConsolidatedCSV for ESXi hosts (vmwareesxihost)...")
    esxi_by_id = collect_unique_esxi_hosts(
        consolidated_csv_dir, end_date_str, n_days, region_filter=region_filter
    )
    print(f"Unique ESXi hosts found: {len(esxi_by_id)}")

    instances_by_region = _group_resources_by_region(
        instances_by_id, region_filter, "instance(s)"
    )
    esxi_by_region = _group_resources_by_region(
        esxi_by_id, region_filter, "ESXi host(s)"
    )

    relationships: list[dict] = []
    active_instance_regions = [r for r in regions if r in instances_by_region]
    if active_instance_regions:
        with ThreadPoolExecutor(
            max_workers=min(len(active_instance_regions), MAX_REGION_WORKERS)
        ) as executor:
            futures = [
                executor.submit(
                    build_relationships_for_region,
                    region,
                    config,
                    signer,
                    instances_by_region[region],
                )
                for region in active_instance_regions
            ]
            for fut in futures:
                relationships.extend(fut.result())

    active_esxi_regions = [r for r in regions if r in esxi_by_region]
    if active_esxi_regions:
        with ThreadPoolExecutor(
            max_workers=min(len(active_esxi_regions), MAX_REGION_WORKERS)
        ) as executor:
            futures = [
                executor.submit(
                    build_esxi_cluster_relationships_for_region,
                    region,
                    config,
                    signer,
                    esxi_by_region[region],
                )
                for region in active_esxi_regions
            ]
            for fut in futures:
                relationships.extend(fut.result())

    relationships.sort(
        key=lambda item: (
            (item.get("master") or {}).get("resourceName") or "",
            (item.get("master") or {}).get("resourceId") or "",
        )
    )

    instance_n = sum(
        1
        for item in relationships
        if ((item.get("master") or {}).get("resourceType") or "").lower()
        == INSTANCE_RESOURCE_TYPE
    )
    cluster_n = sum(
        1
        for item in relationships
        if ((item.get("master") or {}).get("resourceType") or "").lower()
        == SDDC_CLUSTER_RESOURCE_TYPE
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "application_version": application_version,
        "generated_at": datetime.now().isoformat(),
        "window_days": n_days,
        "end_date": end_date_str,
        "instance_count": instance_n,
        "sddc_cluster_count": cluster_n,
        "relationship_count": len(relationships),
        "relationships": relationships,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"\nWrote {len(relationships)} relationship(s) "
        f"({instance_n} instance, {cluster_n} SDDC cluster) to {out_json}"
    )


if __name__ == "__main__":
    main()
