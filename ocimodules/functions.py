"""
Shared helper functions for the OCI Focus Report scripts.

Command line parsing, authentication (config file / instance principals /
delegation token / session token / interactive browser login), Usage API
tenancy config and logging to file.

Authentication, session handling, check_oci_version and MyWriter are taken from
OCI-ScriptFrameWork (https://github.com/RichardORCL/OCI-ScriptFrameWork).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import oci
import requests

# Default profile name used by --interactive (browser based session login)
DefaultSessionProfile = "SESSION"


##########################################################################
# input_command_line
##########################################################################
def input_command_line(help=False):

    parser = argparse.ArgumentParser(formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=80, width=130))
    parser.add_argument('-cp', default="DEFAULT", dest='config_profile', help='Config Profile inside the config file')
    parser.add_argument('-ip', action='store_true', default=False, dest='is_instance_principals', help='Use Instance Principals for Authentication')
    parser.add_argument('-dt', action='store_true', default=False, dest='is_delegation_token', help='Use Delegation Token for Authentication')
    parser.add_argument('-st', action='store_true', default=False, dest='is_security_token', help='Use Session (Security Token) Authentication, created with "oci session authenticate"')
    parser.add_argument('-i', '--interactive', nargs='?', const=DefaultSessionProfile, default="", dest='interactive_profile', metavar='PROFILE',
                        help='Interactive browser login: create or reuse a session token via the OCI CLI ("oci session authenticate"). Optional session profile name, default: {}'.format(DefaultSessionProfile))
    parser.add_argument("-log", nargs='?', const='log.txt', default="", dest='log_file', help="Output also to logfile. If logfile not specified, will log to log.txt")
    parser.add_argument("-year", default=None, dest='year', help="Filter objects by year (appends /year to prefix)")
    parser.add_argument("-month", default=None, dest='month', help="Filter objects by month (requires -year; appends /year/month to prefix)")
    parser.add_argument("-day", default=None, dest='day', help="getFocusReports: day of month (with -year/-month). getRelationships/getResourceNames: last N days of consolidated Focus reports")
    parser.add_argument("-force", action='store_true', default=False, dest='force', help="Re-download all specified files even if already present")
    parser.add_argument("-update", action='store_true', default=False, dest='update', help="Download only new focus reports (newer than current downloaded reports)")
    parser.add_argument("-ns", default="", dest='namespace', help="getFocusReports: Object Storage namespace of the cost reports (overrides focusReports.namespace in config.json)")
    parser.add_argument("-bucket", default="", dest='bucket', help="getFocusReports: bucket of the cost reports (overrides focusReports.bucket in config.json)")
    parser.add_argument("-legacy", action='store_true', default=False, dest='legacy', help="getFocusReports: use the legacy report location (focusReports.legacyNamespace / legacyBucket in config.json)")
    parser.add_argument("-rg", default="", dest='regions', help="Regions to process separated by commas")
    cmd = parser.parse_args()

    # If running in Cloud Shell (OCI_CLI_CLOUD_SHELL=true), default to Delegation Token
    if os.environ.get("OCI_CLI_CLOUD_SHELL", "").lower() == "true":
        print("Running in Cloud Shell..")
        cmd.is_delegation_token = True
        cmd.is_instance_principals = False
        cmd.is_security_token = False
        cmd.interactive_profile = ""
        cmd.config_profile = "DEFAULT"

    # Interactive browser login: session token authentication using the chosen session profile
    elif cmd.interactive_profile:
        cmd.is_security_token = True
        cmd.is_instance_principals = False
        cmd.is_delegation_token = False
        cmd.config_profile = cmd.interactive_profile

    # If OCI_CLI_AUTH=security_token is set (same variable the OCI CLI uses), default to Session Authentication
    elif os.environ.get("OCI_CLI_AUTH", "").lower() == "security_token" and not (cmd.is_instance_principals or cmd.is_delegation_token):
        cmd.is_security_token = True

    if help:
        parser.print_help()

    return cmd


def signer_from_command_line(cmd):
    """create_signer() using all authentication options parsed by input_command_line()."""
    first_region = (getattr(cmd, "regions", "") or "").split(",")[0].strip()
    return create_signer(cmd.config_profile, cmd.is_instance_principals, cmd.is_delegation_token,
                         cmd.is_security_token, interactive=(cmd.interactive_profile != ""), region=first_region)


##########################################################################
# Create signer for Authentication
# Input - config_profile, is_instance_principals, is_delegation_token, is_security_token
#         interactive: create/reuse the session token via the OCI CLI before using it (implies is_security_token)
#         region: region to create the interactive session in (optional, CLI prompts if empty)
# Output - config and signer objects
##########################################################################
def create_signer(config_profile, is_instance_principals, is_delegation_token, is_security_token=False, interactive=False, region=""):

    if interactive:
        is_security_token = True
        ensure_interactive_session(config_profile, region)

    # -----------------------------
    # Instance Principals
    # -----------------------------
    if is_instance_principals:
        try:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            config = {'region': signer.region, 'tenancy': signer.tenancy_id}
            return config, signer

        except Exception as e:
            print("Error obtaining instance principals certificate, aborting: {}".format(e))
            sys.exit(1)

    # -----------------------------
    # Delegation Token (OCI Cloud Shell)
    # -----------------------------
    elif is_delegation_token:

        try:
            # check if env variables OCI_CONFIG_FILE, OCI_CONFIG_PROFILE exist and use them
            env_config_file = os.environ.get('OCI_CONFIG_FILE')
            env_config_section = os.environ.get('OCI_CONFIG_PROFILE')

            if env_config_file is None or env_config_section is None:
                print("*** OCI_CONFIG_FILE and OCI_CONFIG_PROFILE env variables not found, abort. ***")
                print("")
                sys.exit(1)

            config = oci.config.from_file(env_config_file, env_config_section)
            delegation_token_location = config["delegation_token_file"]

            with open(delegation_token_location, 'r') as delegation_token_file:
                delegation_token = delegation_token_file.read().strip()

            # get signer from delegation token
            signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(delegation_token=delegation_token)
            return config, signer

        except KeyError:
            print("* Key Error obtaining delegation_token_file from config profile, aborting")
            sys.exit(1)

        except Exception as e:
            print("Error obtaining delegation token authentication, aborting: {}".format(e))
            sys.exit(1)

    # -----------------------------
    # Session / Security Token authentication
    # Profile created with: oci session authenticate [--profile-name NAME]
    # The profile contains security_token_file, key_file, tenancy and region (no user/fingerprint).
    # https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clitoken.htm
    # -----------------------------
    elif is_security_token:
        try:
            config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                (config_profile if config_profile else oci.config.DEFAULT_PROFILE)
            )
            token_file = config["security_token_file"]
            with open(os.path.expanduser(token_file), 'r') as f:
                token = f.read().strip()

            private_key = oci.signer.load_private_key_from_file(
                config["key_file"],
                pass_phrase=oci.config.get_config_value_or_default(config, "pass_phrase")
            )

            # Check the token has not expired before making any API call
            token_container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
            if not token_container.valid():
                print("The session token in profile '{}' has expired.".format(config_profile))
                print("Refresh it with:  oci session refresh --profile {}".format(config_profile))
                print("or re-authenticate: oci session authenticate --profile-name {}".format(config_profile))
                sys.exit(1)

            # Session profiles have no 'user' key; the user OCID is the 'sub' claim of the token
            if "user" not in config and token_container.get_jwt().get("sub"):
                config["user"] = token_container.get_jwt()["sub"]

            signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
            return config, signer

        except KeyError as e:
            print("Profile '{}' is missing key {} - is this a session profile created with 'oci session authenticate'? aborting".format(config_profile, e))
            sys.exit(1)

        except Exception as e:
            print("Error obtaining session token authentication, aborting: {}".format(e))
            sys.exit(1)

    # -----------------------------
    # config file authentication (~/.oci/config)
    # -----------------------------
    else:
        try:
            config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                (config_profile if config_profile else oci.config.DEFAULT_PROFILE)
            )
            oci.config.validate_config(config)
            signer = oci.signer.Signer(
                tenancy=config["tenancy"],
                user=config["user"],
                fingerprint=config["fingerprint"],
                private_key_file_location=config.get("key_file"),
                pass_phrase=oci.config.get_config_value_or_default(config, "pass_phrase"),
                private_key_content=config.get("key_content")
            )
        except Exception as e:
            print("Error obtaining authentication, did you configure the config file? aborting: {}".format(e))
            sys.exit(1)

        return config, signer


#################################################
# Session profile helpers (interactive browser login)
#################################################
def session_profile_status(profile):
    """
    Inspect a profile in ~/.oci/config.
    Returns (status, expiry) where status is one of:
      "missing"     - profile does not exist
      "not_session" - profile exists but is a regular API key profile (no security_token_file)
      "expired"     - session profile whose token has expired (or token file is unreadable)
      "valid"       - session profile with a valid token
    expiry is a datetime (local time) for session profiles, else None.
    """
    try:
        config = oci.config.from_file(oci.config.DEFAULT_LOCATION, profile)
    except (oci.exceptions.ConfigFileNotFound, oci.exceptions.ProfileNotFound):
        return "missing", None

    if "security_token_file" not in config:
        return "not_session", None

    try:
        with open(os.path.expanduser(config["security_token_file"]), 'r') as f:
            token = f.read().strip()
        container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
        expiry = datetime.fromtimestamp(container.get_jwt()["exp"])
        return ("valid" if container.valid() else "expired"), expiry
    except Exception:
        return "expired", None


def ensure_interactive_session(profile, region=""):
    """
    Make sure a valid session token exists for `profile`, using the OCI CLI:
      - reuse it if still valid
      - try 'oci session refresh' if expired (works up to 24h after login)
      - otherwise run 'oci session authenticate' (opens a browser for login)
    Refuses to touch a profile that is not a session profile. Exits on failure.
    """
    oci_cli = shutil.which("oci")
    if oci_cli is None:
        print("Interactive login requires the OCI CLI ('oci' not found in PATH).")
        print("Install it: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm  (or: pip install oci-cli)")
        sys.exit(1)

    status, expiry = session_profile_status(profile)

    if status == "not_session":
        print("Profile '{}' exists in {} but is not a session profile - refusing to overwrite it.".format(profile, oci.config.DEFAULT_LOCATION))
        print("Choose another session profile name, e.g.:  --interactive MYSESSION")
        sys.exit(1)

    if status == "valid":
        print("Reusing session profile '{}' (token valid until {:%Y-%m-%d %H:%M:%S})".format(profile, expiry))
        return

    if status == "expired":
        print("Session profile '{}' has expired, trying to refresh..".format(profile))
        result = subprocess.run([oci_cli, "session", "refresh", "--profile", profile])
        status, expiry = session_profile_status(profile)
        if result.returncode == 0 and status == "valid":
            print("Session refreshed (token valid until {:%Y-%m-%d %H:%M:%S})".format(expiry))
            return
        print("Refresh not possible, a new browser login is required.")

    print("Starting browser login for session profile '{}'..".format(profile))
    command = [oci_cli, "session", "authenticate", "--profile-name", profile]
    if region:
        command += ["--region", region]
    result = subprocess.run(command)
    if result.returncode != 0:
        print("'oci session authenticate' failed (exit code {}), aborting".format(result.returncode))
        sys.exit(1)

    status, expiry = session_profile_status(profile)
    if status != "valid":
        print("Session profile '{}' is still not usable after login (status: {}), aborting".format(profile, status))
        sys.exit(1)
    print("Session created (token valid until {:%Y-%m-%d %H:%M:%S})".format(expiry))


##########################################################################
# Get tenancy configuration from Usage API
# Raw GET to /20200107/console/getConfig
##########################################################################
def get_tenancy_config(config, signer, region=None, timeout=120):
    """
    Get tenancy configuration from the Usage API.
    Makes a raw GET request to /20200107/console/getConfig.

    Args:
        config: OCI config dict (must have 'tenancy' and 'region')
        signer: OCI signer for request authentication
        region: Optional region override (default: config['region'])
        timeout: Request timeout in seconds (default: 120). A float applies to
            both connect and read; a (connect, read) tuple is also accepted,
            matching UsageapiClient timeout.

    Returns:
        dict: JSON response data from the API
    """
    region = region or config.get("region")
    tenant_id = config.get("tenancy")
    url = f"https://usageapi.{region}.oci.oraclecloud.com/20200107/console/getConfig?tenantId={tenant_id}"
    response = requests.get(url, auth=signer, timeout=timeout)
    response.raise_for_status()
    return response.json()


def get_tenancy_config_counts(config_data):
    """
    Extract region, compartment and service counts from tenancy config (getConfig response).

    Args:
        config_data: JSON dict from get_tenancy_config()

    Returns:
        dict: {"regions": int, "compartments": int, "services": int}
    """
    regions = 0
    compartments = 0
    services = 0
    for item in config_data.get("configList", []):
        key = item.get("key")
        values = item.get("values", [])
        if key == "region":
            regions = len(values)
        elif key == "compartmentId":
            compartments = len(values)
        elif key == "service":
            services = len(values)
    return {"regions": regions, "compartments": compartments, "services": services}


##########################################################################
# Checking SDK Version
# Minimum version requirements for OCI SDK
##########################################################################
def check_oci_version(min_oci_version_required):
    outdated = False

    for i, rl in zip(oci.__version__.split("."), min_oci_version_required.split(".")):
        if int(i) > int(rl):
            break
        if int(i) < int(rl):
            outdated = True
            break

    if outdated:
        print("Your version of the OCI SDK is out-of-date. Please first upgrade your OCI SDK Library by running the command:")
        print("OCI SDK Version : {}".format(oci.__version__))
        print("Min SDK required: {}".format(min_oci_version_required))
        print("pip install --upgrade oci")
        sys.exit(1)


#############################################
# MyWriter to redirect output
#############################################
def CurrentTimeString():
    return time.strftime("%D %H:%M:%S", time.localtime())


class MyWriter:
    """
    Tee-style writer: everything written to it goes to the original stdout
    and is appended to a log file. Install with `sys.stdout = MyWriter(sys.stdout, "log.txt")`.
    """

    def __init__(self, stdout, filename):
        self.stdout = stdout
        self.filename = filename
        self.logfile = open(self.filename, "a", encoding="utf-8")

    def write(self, text):
        self.stdout.write(text)
        self.logfile.write(text)

    def flush(self):
        self.stdout.flush()
        self.logfile.flush()

    def close(self):
        # Only close the log file - never the real stdout
        self.flush()
        self.logfile.close()

    def isatty(self):
        return self.stdout.isatty()

    def __getattr__(self, name):
        # Delegate anything else (encoding, fileno, ...) to the real stdout
        return getattr(self.stdout, name)
