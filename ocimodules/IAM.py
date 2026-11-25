"""
Identity helpers: login, compartment discovery, regions and tenancy name.

Compartment discovery follows the approach of OCI-ScriptFrameWork
(https://github.com/RichardORCL/OCI-ScriptFrameWork): the whole compartment
tree is fetched with ONE paginated list_compartments call
(compartment_id_in_subtree=True, access_level=ANY) and full paths are resolved
in memory, instead of one API call per compartment with a fixed depth limit.
"""

import time

import oci

WaitRefresh = 10
MaxRetries = 10
MaxIDeleteTagIteration = 5

ROOT_PATH = "/root"
DELETED_MARKER = " [deleted]"


class OCICompartments:
    """A compartment together with its full path (e.g. /root/prod/network) and depth level."""

    def __init__(self, details=None, fullpath="", level=0):
        # Instance attributes: the previous class-level attributes were shared by
        # every instance, including one shared Compartment model object.
        self.details = details if details is not None else oci.identity.models.Compartment()
        self.fullpath = fullpath
        self.level = level

    def __repr__(self):
        return "OCICompartments(fullpath={!r}, level={}, id={!r})".format(
            self.fullpath, self.level, getattr(self.details, "id", None))


class CompartmentList(list):
    """
    A plain list of OCICompartments (ACTIVE compartments, root first, sorted by path)
    with two extra lookups:
      by_id     - OCID -> OCICompartments for the ACTIVE compartments in the list
      all_paths - OCID -> full path for every compartment in the tenancy, including
                  DELETED / inactive ones (their segment is marked with " [deleted]").
                  Useful for billing data, which still references removed compartments.
    """

    def __init__(self, items=(), all_paths=None):
        super().__init__(items)
        self.by_id = {c.details.id: c for c in self}
        self.all_paths = dict(all_paths or {})
        for c in self:
            self.all_paths.setdefault(c.details.id, c.fullpath)

    @property
    def deleted_count(self):
        return sum(1 for p in self.all_paths.values() if DELETED_MARKER in p)

    def path_for(self, ocid, include_deleted=True):
        """Full path for an OCID, or None when unknown."""
        comp = self.by_id.get(ocid)
        if comp is not None:
            return comp.fullpath
        if include_deleted:
            return self.all_paths.get(ocid)
        return None


#################################################
# Basic Get Compartments Function
# subtree=True is only valid when rootID is the tenancy OCID and
# returns the whole compartment hierarchy in a single (paginated) call.
#################################################
def GetCompartments(identity, rootID, subtree=False):
    for attempt in range(MaxRetries):
        try:
            return oci.pagination.list_call_get_all_results(
                identity.list_compartments,
                compartment_id=rootID,
                compartment_id_in_subtree=subtree,
                access_level="ANY",
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
            ).data
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                print("API busy.. retry {}/{}".format(attempt + 1, MaxRetries), end="\r")
                time.sleep(WaitRefresh)
            else:
                print("Error listing compartments for {}: {}".format(rootID, e.message))
                return []
    print("Giving up listing compartments for {} after {} retries".format(rootID, MaxRetries))
    return []


def _root_compartment(tenancy_id):
    root = oci.identity.models.Compartment()
    root.id = tenancy_id
    root.name = "root compartment"
    root.lifecycle_state = "ACTIVE"
    root.compartment_id = None
    return root


def build_compartment_tree(tenancy_id, all_compartments, start_compartment=None):
    """
    Build a CompartmentList from the flat result of a subtree list_compartments call.

    - Paths are resolved by walking up the parent chain, so there is no depth limit.
    - Only ACTIVE compartments whose whole parent chain is ACTIVE end up in the list
      (same rule as before: children of a deleted/inactive compartment are skipped).
    - all_paths additionally resolves every non-ACTIVE compartment (and anything below
      it), marking the non-ACTIVE segments with " [deleted]".
    - start_compartment (optional OCID): only return that compartment and its descendants.
    """
    by_id = {c.id: c for c in all_compartments}
    resolved = {tenancy_id: (ROOT_PATH, 0, True)}  # ocid -> (path, level, fully_active)

    def resolve(ocid, seen=()):
        if ocid in resolved:
            return resolved[ocid]
        comp = by_id.get(ocid)
        if comp is None or ocid in seen:
            # parent unknown (not visible) or a cycle; cannot build a path
            return None
        parent = resolve(comp.compartment_id, seen + (ocid,))
        if parent is None:
            return None
        active = comp.lifecycle_state == "ACTIVE"
        name = comp.name if active else comp.name + DELETED_MARKER
        result = (parent[0] + "/" + name, parent[1] + 1, parent[2] and active)
        resolved[ocid] = result
        return result

    items = [OCICompartments(_root_compartment(tenancy_id), ROOT_PATH, 0)]
    all_paths = {}
    for ocid, comp in by_id.items():
        r = resolve(ocid)
        if r is None:
            continue
        all_paths[ocid] = r[0]
        if r[2]:
            items.append(OCICompartments(comp, r[0], r[1]))

    # Sort hierarchically by path components, root stays first
    items.sort(key=lambda c: c.fullpath.split("/"))

    if start_compartment and start_compartment != tenancy_id:
        start = next((c for c in items if c.details.id == start_compartment), None)
        if start is None:
            items = []
        else:
            prefix = start.fullpath + "/"
            items = [c for c in items if c is start or c.fullpath.startswith(prefix)]

    return CompartmentList(items, all_paths)


def GetFullCompartments(config, signer, start_compartment=None):
    """All compartments of the tenancy (one API call), see build_compartment_tree."""
    identity = oci.identity.IdentityClient(config, signer=signer)
    tenancy_id = config["tenancy"]
    all_compartments = GetCompartments(identity, tenancy_id, subtree=True)
    return build_compartment_tree(tenancy_id, all_compartments, start_compartment)


def GetCompartmentFullPath(compartments, ocid, include_deleted=False):
    """
    Given a list of OCICompartments objects and an OCID,
    returns the full path of the compartment that matches the given OCID.
    If not found, returns None.
    With a CompartmentList (as returned by Login / GetFullCompartments) this is a
    dict lookup; include_deleted=True also resolves deleted compartments.
    """
    if isinstance(compartments, CompartmentList):
        return compartments.path_for(ocid, include_deleted=include_deleted)
    for compartment in compartments:
        if getattr(compartment.details, "id", None) == ocid:
            return compartment.fullpath
    return None


def FindCompartment(compartments, value):
    """
    Find a compartment by OCID, full path (e.g. /root/prod/network) or name.
    Returns the first matching OCICompartments object or None.
    """
    for compartment in compartments:
        if compartment.details.id == value or compartment.fullpath == value:
            return compartment
    for compartment in compartments:
        if compartment.details.name == value:
            return compartment
    return None


#################################################
#                 Login                 #
#################################################
def Login(config, signer, startcomp, sso_user=False, get_compartments=False):
    """
    Print who we are logged in as and optionally return the compartment list.
    startcomp: tenancy OCID for the whole tree, or a compartment OCID to limit the list
    to that compartment and its descendants (paths are always full /root/... paths).
    sso_user is kept for backwards compatibility; an unreadable user is never fatal now.
    """
    print("OCI SDK Version: {}".format(oci.__version__))
    identity = oci.identity.IdentityClient(config, signer=signer)
    user_id = config.get("user")
    if user_id:
        try:
            user = identity.get_user(user_id).data
            print("Logged in as: {} @ {}".format(user.description or user.name, config["region"]))
        except oci.exceptions.ServiceError as e:
            # Federated / SSO / session users are often not readable via get_user
            print("Logged in as: {} @ {} (could not read user: {})".format(user_id, config["region"], e.message))
    else:
        print("Logged in as: {} @ {}".format("InstancePrincipal/DelegationToken", config["region"]))

    if not get_compartments:
        return CompartmentList()

    print("Getting compartments...")
    compartments = GetFullCompartments(config, signer, start_compartment=startcomp)
    print("Compartments found: {} active, {} deleted/inactive".format(len(compartments), compartments.deleted_count))
    return compartments


#################################################
#              SubscribedRegions
#################################################
def SubscribedRegions(config, signer):
    identity = oci.identity.IdentityClient(config, signer=signer)
    regionDetails = identity.list_region_subscriptions(tenancy_id=config["tenancy"]).data
    return [detail.region_name for detail in regionDetails]


#################################################
#              GetHomeRegion
#################################################
def GetHomeRegion(config, signer):
    identity = oci.identity.IdentityClient(config, signer=signer)
    regionDetails = identity.list_region_subscriptions(tenancy_id=config["tenancy"]).data
    for reg in regionDetails:
        if reg.is_home_region:
            return str(reg.region_name)
    return ""


#################################################
#              GetTenantName
#################################################
def GetTenantName(config, signer):
    identity = oci.identity.IdentityClient(config, signer=signer)
    tenancy = identity.get_tenancy(config['tenancy']).data
    return tenancy.name
