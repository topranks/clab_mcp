"""
topology_validator.py - allowlist validation for containerlab topologies
submitted by an AI agent through the MCP server's deployLab tool.

Philosophy: reject anything not explicitly allowed. New containerlab
features then stay blocked until you decide they're safe.

Usage in clab_mcp_server.py, at the top of deployLab():
    from topology_validator import validate_topology, TopologyError
    try:
        validate_topology(topology_content)
    except TopologyError as e:
        return f"Topology rejected: {e}"
"""
import ipaddress
import os
import re

# ---- policy: edit these to match your labs --------------------------------

ALLOWED_IMAGES = {
    "linux": {"debian:clab"},
    "cisco_xrd": {"ios-xr/xrd-control-plane:latest"},
    "frr": {"quay.io/frrouting/frr:10.2.1"},
}

# Bind destinations allowed per kind (inside the container).
ALLOWED_BIND_DESTS = {
    "linux": {"/etc/frr"},
    "frr": {"/etc/frr"},
}

# Kinds whose nodes may use exec, and the commands they may run (see _check_exec).
EXEC_KINDS = {"linux"}
SERVICE_COMMANDS = {("/etc/init.d/frr", "start"), ("/etc/init.d/frr", "restart")}

# Management network: only these host bridges, and addresses inside these subnets.
ALLOWED_MGMT_BRIDGES = {"clab"}
MGMT_SUBNETS = [ipaddress.ip_network("172.20.20.0/24"),
                ipaddress.ip_network("3fff:172:20:20::/64")]

ALLOWED_TOP_KEYS = {"name", "mgmt", "topology"}
ALLOWED_MGMT_KEYS = {"network", "bridge"}
ALLOWED_TOPOLOGY_KEYS = {"kinds", "nodes", "links"}  # no defaults/groups
ALLOWED_KIND_KEYS = {"image"}                        # kinds may only set an image
ALLOWED_NODE_KEYS = {"kind", "image", "binds", "exec", "startup-config",
                     "mgmt-ipv4", "mgmt-ipv6"}
ALLOWED_BIND_MODES = {"ro", "rw"}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,30}$")
SHELL_CHARS = set(";&|`$<>(){}[]*?!~\\'\"\n\r\t#")


class TopologyError(ValueError):
    pass


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise TopologyError(f"{where} must be an object")
    extra = set(obj) - allowed
    if extra:
        raise TopologyError(f"{where}: field(s) not allowed: {', '.join(sorted(extra))}")


def _check_rel_path(path, where):
    """A path on the host must stay inside the lab directory."""
    if not isinstance(path, str) or not path:
        raise TopologyError(f"{where}: path must be a non-empty string")
    if "://" in path or any(c in path for c in "$~`\\"):
        raise TopologyError(f"{where}: URLs, variables, '~' and backslashes are not allowed")
    if os.path.isabs(path):
        raise TopologyError(f"{where}: absolute host paths are not allowed")
    norm = os.path.normpath(path)
    if norm in (".", "..") or norm.startswith("../"):
        raise TopologyError(f"{where}: path must stay inside the lab directory")
    return norm


def _check_bind(bind, kind, where):
    if not isinstance(bind, str):
        raise TopologyError(f"{where}: bind must be a string 'src:dst[:mode]'")
    parts = bind.split(":")
    if len(parts) not in (2, 3):
        raise TopologyError(f"{where}: expected 'src:dst' or 'src:dst:mode'")
    _check_rel_path(parts[0], where)
    if os.path.normpath(parts[1]) not in ALLOWED_BIND_DESTS.get(kind, set()):
        raise TopologyError(f"{where}: bind destination {parts[1]!r} not allowed for kind {kind!r}")
    if len(parts) == 3 and parts[2] not in ALLOWED_BIND_MODES:
        raise TopologyError(f"{where}: bind mode must be one of {sorted(ALLOWED_BIND_MODES)}")


def _check_exec(cmd, where):
    """Only a few fixed command shapes, checked argument by argument."""
    if not isinstance(cmd, str) or any(c in SHELL_CHARS for c in cmd):
        raise TopologyError(f"{where}: exec {cmd!r} contains shell syntax")
    argv = cmd.split()
    if argv[:1] == ["ip"] and argv[1:2] in (["-4"], ["-6"]):
        argv = ["ip"] + argv[2:]

    # ip addr add <address/prefix> dev <iface>
    if len(argv) == 6 and argv[0] == "ip" and argv[1] in ("addr", "address") \
            and argv[2] == "add" and argv[4] == "dev":
        try:
            ipaddress.ip_interface(argv[3])
        except ValueError:
            raise TopologyError(f"{where}: invalid address in {cmd!r}") from None
        if not IFACE_RE.match(argv[5]):
            raise TopologyError(f"{where}: invalid interface in {cmd!r}")
        return
    # ip link set <iface> up|down
    if len(argv) == 5 and argv[:3] == ["ip", "link", "set"] and argv[4] in ("up", "down"):
        if not IFACE_RE.match(argv[3]):
            raise TopologyError(f"{where}: invalid interface in {cmd!r}")
        return
    # service start/restart
    if tuple(argv) in SERVICE_COMMANDS:
        return
    raise TopologyError(f"{where}: exec command not allowed: {cmd!r}")


def _check_mgmt_ip(value, version, where):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        raise TopologyError(f"{where}: invalid mgmt address {value!r}") from None
    if addr.version != version or not any(addr in n for n in MGMT_SUBNETS):
        raise TopologyError(f"{where}: mgmt address {value} outside the allowed subnets")


def validate_topology(topo):
    _check_keys(topo, ALLOWED_TOP_KEYS, "top level")
    if not NAME_RE.match(str(topo.get("name", ""))):
        raise TopologyError("name must be 1-63 letters, digits, '-' or '_'")

    if "mgmt" in topo:
        mgmt = topo["mgmt"]
        _check_keys(mgmt, ALLOWED_MGMT_KEYS, "mgmt")
        if "network" in mgmt and not NAME_RE.match(str(mgmt["network"])):
            raise TopologyError("mgmt.network: invalid name")
        if "bridge" in mgmt and mgmt["bridge"] not in ALLOWED_MGMT_BRIDGES:
            raise TopologyError(f"mgmt.bridge {mgmt['bridge']!r} not allowed")

    t = topo.get("topology")
    _check_keys(t, ALLOWED_TOPOLOGY_KEYS, "topology")

    kinds = t.get("kinds", {})
    if not isinstance(kinds, dict):
        raise TopologyError("topology.kinds must be an object")
    for kname, kdef in kinds.items():
        _check_keys(kdef, ALLOWED_KIND_KEYS, f"kinds.{kname}")
        if kname not in ALLOWED_IMAGES:
            raise TopologyError(f"kinds.{kname}: kind not allowed")
        if "image" in kdef and kdef["image"] not in ALLOWED_IMAGES[kname]:
            raise TopologyError(f"kinds.{kname}: image {kdef['image']!r} not approved")

    nodes = t.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise TopologyError("topology.nodes must be a non-empty object")

    for name, node in nodes.items():
        where = f"node {name!r}"
        if not NAME_RE.match(name):
            raise TopologyError(f"{where}: invalid node name")
        _check_keys(node, ALLOWED_NODE_KEYS, where)

        kind = node.get("kind")
        if kind not in ALLOWED_IMAGES:
            raise TopologyError(f"{where}: kind {kind!r} not allowed")
        image = node.get("image", kinds.get(kind, {}).get("image"))
        if image not in ALLOWED_IMAGES[kind]:
            raise TopologyError(f"{where}: image {image!r} not approved for {kind!r}")

        binds = node.get("binds", [])
        if not isinstance(binds, list):
            raise TopologyError(f"{where}: binds must be a list")
        for b in binds:
            _check_bind(b, kind, where)

        execs = node.get("exec", [])
        if execs and kind not in EXEC_KINDS:
            raise TopologyError(f"{where}: exec not allowed for kind {kind!r}")
        if not isinstance(execs, list):
            raise TopologyError(f"{where}: exec must be a list")
        for cmd in execs:
            _check_exec(cmd, where)

        if "startup-config" in node:
            _check_rel_path(node["startup-config"], f"{where} startup-config")
        if "mgmt-ipv4" in node:
            _check_mgmt_ip(node["mgmt-ipv4"], 4, where)
        if "mgmt-ipv6" in node:
            _check_mgmt_ip(node["mgmt-ipv6"], 6, where)

    # Links may only join interfaces of nodes defined above. This blocks the
    # special endpoints (host:, macvlan:, mgmt-net:, bridges) that attach
    # lab interfaces to the host's own network.
    links = t.get("links", [])
    if not isinstance(links, list):
        raise TopologyError("topology.links must be a list")
    for i, link in enumerate(links):
        where = f"link {i}"
        _check_keys(link, {"endpoints"}, where)
        eps = link.get("endpoints")
        if not isinstance(eps, list) or len(eps) != 2:
            raise TopologyError(f"{where}: needs exactly two endpoints")
        for ep in eps:
            node, sep, iface = str(ep).partition(":")
            if not sep or node not in nodes or not IFACE_RE.match(iface):
                raise TopologyError(f"{where}: endpoint {ep!r} must be '<defined node>:<interface>'")
