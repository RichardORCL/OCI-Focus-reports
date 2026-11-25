"""
Download the OCI FOCUS cost reports from Object Storage into FocusReports/FOCUS Reports/.

Source location comes from config.json "focusReports" (namespace from the console link,
bucket internal-<tenancy OCID>; objects "FOCUS Reports/YYYY/MM/DD/[<ocid>/]<file>.csv.gz").
Files are decompressed and an oci_CompartmentPath column (full compartment path) is added.
makeDailyReports.py then builds the daily/monthly reports under FocusReports/Consolidated*/.

Usage:
    python getFocusReports.py                         # all available reports
    python getFocusReports.py -update                 # only new reports (used by runall)
    python getFocusReports.py -year 2026 -month 11    # one month (add -day DD for one day)
    python getFocusReports.py -force                  # re-download files already present
    python getFocusReports.py -legacy | -ns NS -bucket BUCKET   # other report location
Authentication options: see README (-cp, -ip, -dt, -st, -i).
"""
import sys
import os
import gzip
import csv
from datetime import date, datetime
import oci

from ocimodules.functions import input_command_line, signer_from_command_line, check_oci_version, MyWriter
from ocimodules.IAM import Login
from ocimodules.settings import load_settings, resolve

# Disable OCI CircuitBreaker feature
oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

#################################################
#           Application Configuration           #
#################################################
min_version_required = "2.164.0"
application_version = "11.03.2026"


##########################################################################
# Main Program
##########################################################################

print ("OCI - OCVS Focus Reports")
print ("This utility help you get focus reports for OCVS")
print ("============================================================================================")
print ("")

# Check command line parameters
cmd = input_command_line()

# if logging to file, overwrite default print function to also write to file
if cmd.log_file != "":
    writer = MyWriter(sys.stdout, cmd.log_file)
    sys.stdout = writer

#################################################
# oci config and "login" check
######################################################
check_oci_version(min_version_required)

config, signer = signer_from_command_line(cmd)
tenant_id = config['tenancy']

compartments= Login(config, signer, tenant_id, get_compartments=True)
print(f"Current configured region is: {config['region']}")

# ocid -> fullpath lookup. Includes deleted/inactive compartments (marked " [deleted]"),
# because billing data keeps referencing compartments after they are removed.
compartment_id_to_path = dict(compartments.all_paths)

#################################################
# List all objects from Object Storage
#################################################
# Report location comes from config.json "focusReports" (see ocimodules/settings.py).
# Namespace: Oracle-owned, tenancy specific (focusReports.namespace); bucket "internal-<tenancy OCID>", files under
# "FOCUS Reports/YYYY/MM/DD/<ocid>/" - the location the OCI console (Billing > Cost and
# Usage Reports) links to. The legacy location (namespace "bling", bucket = tenancy OCID)
# still exists, but its files no longer contain cost data.
# Command line -ns / -bucket / -legacy override config.json.
focus_settings = load_settings()["focusReports"]

object_storage_client = oci.object_storage.ObjectStorageClient(config, signer=signer)

if cmd.legacy:
    namespace = resolve(focus_settings["legacyNamespace"], tenant_id)
    bucket_name = resolve(focus_settings["legacyBucket"], tenant_id)
else:
    namespace = resolve(focus_settings["namespace"], tenant_id)
    bucket_name = resolve(focus_settings["bucket"], tenant_id)
if cmd.namespace:
    namespace = cmd.namespace
if cmd.bucket:
    bucket_name = cmd.bucket

if not namespace or namespace.startswith("<"):
    print("Error: no cost report namespace configured.")
    print("Set focusReports.namespace in config.json (or use -ns). Find it in the OCI console:")
    print("Billing & Cost Management > Cost and Usage Reports: the part after /n/ in the report URL.")
    sys.exit(-1)

if cmd.month and not cmd.year:
    print("Error: -month can only be used in conjunction with -year")
    sys.exit(-1)

if cmd.day and (not cmd.year or not cmd.month):
    print("Error: -day can only be used in conjunction with -year and -month")
    sys.exit(-1)

prefix = "FOCUS Reports"
if cmd.year:
    year_val = int(cmd.year)
    year_str = f"{year_val + 2000:04d}" if year_val < 100 else f"{year_val:04d}"
    prefix = f"{prefix}/{year_str}"
    if cmd.month:
        prefix = f"{prefix}/{int(cmd.month):02d}"
        if cmd.day:
            prefix = f"{prefix}/{int(cmd.day):02d}"

print(f"\nListing objects from namespace '{namespace}', bucket '{bucket_name}' (prefix: {prefix})...")
print("-" * 80)

all_objects = []
start = None

while True:
    try:
        list_response = object_storage_client.list_objects(
            namespace_name=namespace,
            bucket_name=bucket_name,
            prefix=prefix,
            start=start,
            limit=1000,
            fields="name,size,timeCreated,timeModified"
        )
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            print(f"Error: bucket '{bucket_name}' in namespace '{namespace}' not found or not authorized.")
            if not cmd.legacy:
                print("Use -legacy to read the old location (focusReports.legacyNamespace / legacyBucket),")
                print("or set focusReports.namespace / bucket in config.json, or use -ns / -bucket.")
            sys.exit(-1)
        raise
    objects = list_response.data.objects
    all_objects.extend(objects)

    for obj in objects:
        size_str = f"{obj.size:,}" if obj.size else "0"
        print(f"  {obj.name}  (size: {size_str} bytes, modified: {obj.time_modified})")

    start = list_response.data.next_start_with
    if start is None:
        break

print("-" * 80)
print(f"Total objects retrieved: {len(all_objects)}")

#################################################
# Filter objects for -update (only newer than latest downloaded)
#################################################
download_dir = "FocusReports"
# Files for a date keep arriving for a few days after that date (late usage), so in
# update mode the last few date folders are re-checked; existing files are skipped below.
UPDATE_LOOKBACK_DAYS = int(focus_settings.get("updateLookbackDays", 3))


def report_date_key(path):
    """(YYYY, MM, DD) from 'FOCUS Reports/YYYY/MM/DD/[<ocid>/]file', or None.
    Works for both the legacy layout and the layout with a tenancy OCID sub-folder."""
    parts = path.replace("\\", "/").split("/")
    try:
        return (int(parts[1]), int(parts[2]), int(parts[3]))
    except (ValueError, IndexError):
        return None


if cmd.update:
    latest_date = None
    focus_dir = os.path.join(download_dir, "FOCUS Reports")
    if os.path.exists(focus_dir):
        for root, dirs, files in os.walk(focus_dir):
            for f in files:
                if not f.endswith(".csv") and not f.endswith(".gz"):
                    continue
                rel = os.path.relpath(os.path.join(root, f), download_dir)
                key = report_date_key(rel)
                if key and (latest_date is None or key > latest_date):
                    latest_date = key
    if latest_date:
        cutoff = date(*latest_date).toordinal() - UPDATE_LOOKBACK_DAYS

        def is_recent(o):
            key = report_date_key(o.name)
            return key is not None and date(*key).toordinal() >= cutoff

        all_objects = [obj for obj in all_objects if is_recent(obj)]
        print(f"Update mode: latest local date={latest_date[0]:04d}-{latest_date[1]:02d}-{latest_date[2]:02d}, "
              f"checking {len(all_objects)} objects from the last {UPDATE_LOOKBACK_DAYS} days onwards")
    else:
        print("Update mode: no existing FOCUS reports found, will download all")

#################################################
# Download objects to FocusReports folder
#################################################
downloaded_count = 0
for obj in all_objects:
    # Final path: .gz files become .csv after decompression
    local_path = os.path.join(download_dir, obj.name)
    final_path = local_path[:-3] if local_path.endswith('.gz') else local_path

    # Skip if already downloaded (unless -force)
    if not cmd.force and os.path.exists(final_path):
        print(f"Skipped (already exists): {final_path}")
        continue

    local_dir = os.path.dirname(local_path)
    os.makedirs(local_dir, exist_ok=True)

    get_response = object_storage_client.get_object(
        namespace_name=namespace,
        bucket_name=bucket_name,
        object_name=obj.name
    )

    with open(local_path, 'wb') as f:
        for chunk in get_response.data.raw.stream(1024 * 1024, decode_content=False):
            f.write(chunk)

    downloaded_count += 1

    if local_path.endswith('.gz'):
        csv_path = local_path[:-3]
        with gzip.open(local_path, 'rb') as gz_in:
            with open(csv_path, 'wb') as csv_out:
                csv_out.write(gz_in.read())
        os.remove(local_path)
        print(f"Downloaded and decompressed: {csv_path}")
        path_to_enrich = csv_path
    else:
        print(f"Downloaded: {local_path}")
        path_to_enrich = local_path

    # Add oci_CompartmentPath column after oci_CompartmentName
    if path_to_enrich.endswith('.csv'):
        with open(path_to_enrich, 'r', encoding='utf-8', newline='') as f_in:
            reader = csv.DictReader(f_in)
            if 'oci_CompartmentName' in reader.fieldnames:
                fieldnames = list(reader.fieldnames)
                comp_name_idx = fieldnames.index('oci_CompartmentName')
                if 'oci_CompartmentPath' not in fieldnames:
                    fieldnames.insert(comp_name_idx + 1, 'oci_CompartmentPath')
                rows = list(reader)
                with open(path_to_enrich, 'w', encoding='utf-8', newline='') as f_out:
                    csv_writer = csv.DictWriter(f_out, fieldnames=fieldnames, extrasaction='ignore')
                    csv_writer.writeheader()
                    for row in rows:
                        comp_id = row.get('oci_CompartmentId', '')
                        row['oci_CompartmentPath'] = compartment_id_to_path.get(comp_id, '')
                        csv_writer.writerow(row)

    # Set file mtime to object's time_modified (for -update to work on next run)
    obj_ts = None
    if obj.time_modified:
        obj_ts = obj.time_modified.timestamp() if hasattr(obj.time_modified, 'timestamp') else obj.time_modified
    else:
        # Fallback: parse date from path (FOCUS Reports/YYYY/MM/DD/[<ocid>/]...)
        key = report_date_key(obj.name)
        if key:
            obj_ts = datetime(*key, 23, 59, 59).timestamp()
    if obj_ts is not None:
        try:
            os.utime(path_to_enrich, (obj_ts, obj_ts))
        except (OSError, TypeError):
            pass

print("-" * 80)
print(f"Downloaded {downloaded_count} objects to {download_dir}/")

