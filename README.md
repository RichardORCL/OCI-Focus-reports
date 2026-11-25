# OCI Focus Reports

Cost and consumption dashboards for Oracle Cloud Infrastructure (OCI), built from the
[FOCUS](https://focus.finops.org/) cost reports that OCI generates for every tenancy.

The scripts download the FOCUS reports, consolidate the hourly rows into one row per
resource and SKU per day, and generate static HTML dashboards:

- **Home page** with a 30-day stacked consumption chart by service
- **Daily and monthly reports** per resource, grouped by service, compartment, owner
  and "master" resource (for example an instance together with its volumes and backups)
- **Per-user dashboards** based on the creator tag of each resource
- **Storage report** of unattached block and boot volumes, with their 30-day cost
  (optionally tags them for scheduled deletion)

The HTML files can be opened locally or uploaded to an Object Storage bucket.

## Prerequisites

- Python 3.8 or later
- Python packages from `requirements.txt` (OCI Python SDK 2.164.0 or later, `requests`):

  ```
  pip install -r requirements.txt
  ```
- An OCI user, instance principal or Cloud Shell session with the permissions below
- Optional: the OCI CLI (`oci`), for `uploadReports.py` and for interactive (`-i`) login

### OCI permissions

The cost reports are read from an Oracle-owned Object Storage namespace. Tenancy
administrators can read them by default. Other users need the cross-tenancy policy from
the OCI documentation
([Cost and Usage Reports](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm))
in the root compartment, for example:

```
define tenancy usage-report as ocid1.tenancy.oc1..aaaaaaaaned4fkpkisbwjlr56u7cj63lf3wffbilvqknstgtvzub7vhqkggq
endorse group <your-group> to read objects in tenancy usage-report
```

The other scripts need read access in the tenancy, for example:

```
allow group <your-group> to inspect compartments in tenancy
allow group <your-group> to read instance-family in tenancy
allow group <your-group> to read volume-family in tenancy
allow group <your-group> to read vmware-family in tenancy
allow group <your-group> to read all-resources in tenancy
```

`read all-resources` is used by `getResourceNames.py` (Resource Search) to look up names
of resources. Tagging unattached volumes (`SetDeletionForUnattachedDisk`) also needs
`use volume-family` and `use tag-namespaces` for the tag namespace in `config.json`.
Uploading the reports needs `manage objects` on the upload bucket.

## Quick start

```
pip install -r requirements.txt
copy config.example.json config.json        (Linux: cp config.example.json config.json)
python getFocusReports.py -year 2026 -month 11
python makeDailyReports.py
```

Then open `FocusReports/ConsolidatedHTML/index.html` (after `makeHomeReport.py`), or run
everything at once, see [Running everything](#running-everything).

### Python virtual environment (Linux / macOS / OCI Cloud Shell)

Installing the packages in a virtual environment keeps them separate from the system Python:

```bash
cd OCI-Focus-reports
python3 -m venv .venv
source .venv/bin/activate          # prompt now starts with (.venv)
pip install --upgrade pip
pip install -r requirements.txt
cp config.example.json config.json
./runall.sh
```

`runall.sh` activates `.venv` automatically when it exists in the project folder, so it
also works from cron without activating anything first. Run `deactivate` to leave the
environment in your shell. To use another interpreter, set `PYTHON`:

```bash
PYTHON=/usr/bin/python3.11 ./runall.sh
```

If `python3 -m venv` reports that `ensurepip` is not available, install the venv package
first: `sudo apt install python3-venv` (Debian/Ubuntu). On Oracle Linux / RHEL venv is part
of the `python3` package; for a newer Python install for example `python3.11` and use
`python3.11 -m venv .venv`.

On Windows: `python -m venv .venv` and `.venv\Scripts\activate`.

## Scripts

Run them in this order (this is what `runall.example.bat` / `runall.sh` do):

| Script | Calls OCI | What it does |
|---|---|---|
| `getFocusReports.py` | yes | Downloads the FOCUS cost reports to `FocusReports/FOCUS Reports/YYYY/MM/DD/` and adds the full compartment path (`oci_CompartmentPath`). `-update` downloads only new files; `-year`/`-month`/`-day` select a period. |
| `getStorageReport.py` | yes | Lists unattached block and boot volumes in all subscribed regions, adds their 30-day cost and writes `consolidateReports/StorageReport/unattached_volumes.json`. |
| `getResourceNames.py` | yes | Looks up missing resource names with Resource Search and writes `relationships/oci-resource-names.json`. `-day N` = last N days of reports. |
| `getRelationships.py` | yes | Links instances to their volumes and backups, and OCVS ESXi hosts to their SDDC cluster. Writes `relationships/oci-resource-relations.json`. |
| `makeDailyReports.py` | no | Consolidates the FOCUS files into daily and monthly CSV and HTML reports. Options: `-update`, `-force` (rebuild all), `-anon` (anonymise owner names). |
| `makePerUserReport.py` | no | Per-user dashboards (last day and last 30 days) in `ConsolidatedHTML/users/`. |
| `makeStorageReport.py` | no | `ConsolidatedHTML/storage.html` from `unattached_volumes.json`. |
| `makeHomeReport.py` | no | `ConsolidatedHTML/index.html` with the 30-day chart and the navigation menu. |
| `uploadReports.py` | CLI | Uploads `ConsolidatedHTML` to the bucket in `config.json` (`upload.bucket`). Does nothing when no bucket is set. |

Library modules: `consolidateReports.py` (consolidation and HTML helpers, also runnable on
its own), `perUserReport.py`, `reportNav.py` (shared top menu), and `ocimodules/`
(command line, authentication, compartments, settings).

## Output

Everything below is generated, contains billing data, and is excluded from git:

```
FocusReports/
  FOCUS Reports/YYYY/MM/DD/[<ocid>/]*.csv     downloaded FOCUS reports
  ConsolidatedCSV/YYYY/MM/YYYY-MM-DD.csv      daily consolidated report
  ConsolidatedCSV/YYYY/YYYY-MM.csv            monthly consolidated report
  ConsolidatedHTML/                           dashboards (index.html, daily, monthly,
                                              users/, storage.html)
consolidateReports/StorageReport/             unattached_volumes.json
relationships/                                resource names and relations (JSON)
```

`oci_focus_schema.md` describes the columns of the OCI FOCUS reports.

## Authentication

All `get*.py` scripts share the same options (from OCI-ScriptFrameWork):

| Option | Authentication |
|---|---|
| *(none)* / `-cp PROFILE` | API key profile in `~/.oci/config` (default `DEFAULT`) |
| `-ip` | Instance Principals |
| `-dt` | Delegation Token (auto-selected in OCI Cloud Shell) |
| `-st -cp PROFILE` | Session token profile created with `oci session authenticate` |
| `-i` / `--interactive [PROFILE]` | Browser login via the OCI CLI; reuses or refreshes the session profile (default `SESSION`) |

`OCI_CLI_AUTH=security_token` in the environment also selects session authentication.

## Compartments

The compartment tree is read with a single `list_compartments` call over the whole
tenancy and full paths (`/root/prod/network`) are resolved in memory. Billing rows that
point to a deleted compartment still get a path in `oci_CompartmentPath`, with the
deleted part marked, for example `/root/projects/old-poc [deleted]`.

## Configuration

Settings are read from `config.json` in the project root. This file is environment
specific and is **not** tracked in git. To get started:

```
copy config.example.json config.json
copy runall.example.bat runall.bat
```

Keys that are missing from `config.json` fall back to built-in defaults
(`ocimodules/settings.py`). If `config.json` is missing, the scripts print a note and
use the defaults.

| Key | Default | Used by | Meaning |
|---|---|---|---|
| `focusReports.namespace` | *(required)* | getFocusReports | Object Storage namespace of the cost reports. This is an Oracle-owned namespace that is specific to your tenancy (it is **not** the namespace of your own tenancy). Find it in the OCI console under Billing & Cost Management > Cost and Usage Reports: open a report and take the part after `/n/` in its URL (`https://objectstorage.<region>.oraclecloud.com/n/<namespace>/b/...`). |
| `focusReports.bucket` | `internal-{tenancy}` | getFocusReports | Bucket of the cost reports. `{tenancy}` is replaced by the tenancy OCID of the OCI config profile. |
| `focusReports.legacyNamespace` | `bling` | getFocusReports `-legacy` | Old report location. Its files no longer contain cost data. |
| `focusReports.legacyBucket` | `{tenancy}` | getFocusReports `-legacy` | Bucket of the old report location. |
| `focusReports.updateLookbackDays` | `3` | getFocusReports `-update` | Re-check this many days before the latest downloaded day, because report files for a day keep arriving for a few days. |
| `upload.bucket` | `""` | uploadReports | Bucket to upload the generated HTML reports to. Empty = upload disabled. |
| `upload.namespace` | `""` | uploadReports | Namespace of the upload bucket. Empty = the namespace of your tenancy. |
| `upload.sourceDir` | `FocusReports/ConsolidatedHTML` | uploadReports | Folder that is uploaded. |
| `dailyReportStaleCheckDays` | `5` | makeDailyReports | Only the last N days are checked for changed input files. `0` = check all days. |
| `tags.createdBy` | `Owner.CreatedBy` | report scripts | Defined tag (`Namespace.Key`) holding the creator of a resource. |
| `tags.createdDate` | `Owner.CreatedDate` | report scripts | Defined tag holding the creation date of a resource. |
| `usdConversionRate` | *(none)* | report scripts | Multiplier from the billing currency to USD, for example `0.012` for INR. Not set = costs are shown as billed. |
| `DayMargin` | `1` | makePerUserReport | Number of days before the most recent day to use for the per-user "last day" report. |
| `SetDeletionForUnattachedDisk` | `false` | getStorageReport | Tag unattached block and boot volumes for scheduled deletion. |
| `unattachedVolumeDeletionTags.namespace` | `Optimization` | getStorageReport | Tag namespace for the deletion tags. |
| `unattachedVolumeDeletionTags.scheduleForDeletionKey` | `ScheduleForDeletion` | getStorageReport | Tag key that marks a volume for deletion. |
| `unattachedVolumeDeletionTags.scheduleForDeletionValue` | `True` | getStorageReport | Value set on that tag. |
| `unattachedVolumeDeletionTags.nextDeletionScheduleKey` | `NextDeletionSchedule` | getStorageReport | Tag key for the planned deletion date. |
| `unattachedVolumeDeletionTags.deletionScheduleDaysAhead` | `14` | getStorageReport | Days from now for the planned deletion date. |

The command line options `-ns`, `-bucket` and `-legacy` of `getFocusReports.py`
override the `focusReports` settings for a single run.

## Running everything

- Windows: copy `runall.example.bat` to `runall.bat` and run it.
- Linux / macOS / OCI Cloud Shell: `./runall.sh` (needs `python3`). Authentication options
  (`-cp PROFILE`, `-ip`, `-dt`, `-st`, `-i`, `-rg`, `-log`) go to all `get*.py` scripts; download
  options (`-year`, `-month`, `-day`, `-force`, `-update`) go to `getFocusReports.py` only
  (default `-update`). Example: `./runall.sh -cp MYPROFILE -year 2026 -month 10`.
- On the first run (no consolidated reports yet) the daily reports are built right after the
  download, so the enrichment scripts have data to work with.
  The script stops at the first failing step. In OCI Cloud Shell the delegation token is
  used automatically. Set `PYTHON=...` to use another Python interpreter.
