"""
Uploads the generated HTML reports to an Object Storage bucket using the OCI CLI
(oci os object bulk-upload). Bucket, optional namespace and source folder come from
config.json "upload". Does nothing when upload.bucket is empty.

Usage:
    python uploadReports.py [extra OCI CLI options, e.g. --profile MYPROFILE]
"""
import shutil
import subprocess
import sys
from pathlib import Path

from ocimodules.settings import load_settings, PROJECT_ROOT


def main():
    upload = load_settings()["upload"]
    bucket = str(upload.get("bucket") or "").strip()
    if not bucket:
        print("Upload skipped: no upload.bucket set in config.json")
        return 0

    src = Path(upload.get("sourceDir") or "FocusReports/ConsolidatedHTML")
    if not src.is_absolute():
        src = PROJECT_ROOT / src
    if not src.is_dir():
        print(f"Upload skipped: source folder not found: {src}")
        return 1

    oci_cli = shutil.which("oci")
    if not oci_cli:
        print("Error: OCI CLI ('oci') not found in PATH")
        return 1

    cmd = [oci_cli, "os", "object", "bulk-upload",
           "--bucket-name", bucket,
           "--src-dir", str(src),
           "--overwrite",
           "--content-type", "text/html"]
    namespace = str(upload.get("namespace") or "").strip()
    if namespace:
        cmd += ["--namespace", namespace]
    cmd += sys.argv[1:]

    print(f"Uploading {src} to bucket '{bucket}'...")
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
