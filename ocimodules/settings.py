"""
Shared application settings (config.json in the project root).

config.json holds the environment-specific settings and is not tracked in git.
Copy config.example.json to config.json and adjust it. Keys missing from config.json
fall back to DEFAULTS below, so scripts keep working with a partial config.
"""
import copy
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
EXAMPLE_CONFIG_FILE = PROJECT_ROOT / "config.example.json"

DEFAULTS = {
    # Where getFocusReports.py downloads the FOCUS cost reports from.
    # "{tenancy}" is replaced by the tenancy OCID from the OCI config profile.
    "focusReports": {
        "namespace": "",  # required: see README "Configuration"
        "bucket": "internal-{tenancy}",
        "legacyNamespace": "bling",
        "legacyBucket": "{tenancy}",
        "updateLookbackDays": 3,
    },
    # Bucket that uploadReports.py uploads the generated HTML reports to ("" = disabled).
    "upload": {
        "namespace": "",
        "bucket": "",
        "sourceDir": "FocusReports/ConsolidatedHTML",
    },
    "dailyReportStaleCheckDays": 5,
    "tags": {
        "createdBy": "Owner.CreatedBy",
        "createdDate": "Owner.CreatedDate",
    },
    "usdConversionRate": None,
    "DayMargin": 1,
    "SetDeletionForUnattachedDisk": False,
    "unattachedVolumeDeletionTags": {
        "namespace": "Optimization",
        "scheduleForDeletionKey": "ScheduleForDeletion",
        "nextDeletionScheduleKey": "NextDeletionSchedule",
        "deletionScheduleDaysAhead": 14,
        "scheduleForDeletionValue": "True",
    },
}

_cache = None


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(reload=False):
    """Return DEFAULTS deep-merged with config.json. Cached after the first call."""
    global _cache
    if _cache is not None and not reload:
        return _cache
    settings = copy.deepcopy(DEFAULTS)
    if not CONFIG_FILE.exists():
        print(f"Note: {CONFIG_FILE.name} not found, using default settings. "
              f"Copy {EXAMPLE_CONFIG_FILE.name} to {CONFIG_FILE.name} and adjust it.", file=sys.stderr)
    else:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                _deep_merge(settings, user)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {CONFIG_FILE.name} ({e}), using default settings.", file=sys.stderr)
    _cache = settings
    return settings


def resolve(template, tenancy_id):
    """Replace {tenancy} in a configured name with the tenancy OCID."""
    return str(template or "").replace("{tenancy}", tenancy_id)
