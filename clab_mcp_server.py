#!/usr/bin/env python3
"""ContainerLab MCP server, hardened for use by AI agents.

Based on seanerama/clab-mcp-server. Changes from the original:
  - runs as an HTTP service (default 127.0.0.1:9000/mcp) instead of stdio,
    so it can run as its own user with credentials the agent can't read
  - no `authenticate` tool: credentials only ever come from the environment,
    and there are no built-in default credentials for the API
  - deployLab validates topologies with topology_validator.py
  - deployLabFile deploys a topology file already in the workspace
  - execCommand only runs allowlisted commands (see EXEC_ALLOWLIST)
  - configureNode applies configuration: via exec for native container
    kinds (FRR, cEOS, XRd, cRPD), via SSH only for vrnetlab (VM-based) kinds
  - sshCommand runs CLI commands on vrnetlab nodes, which exec can't reach
  - lab names are validated before being used in API URLs
  - ignores proxy environment variables (the API server is local)

Environment:
  CLAB_API_SERVER_URL   default http://localhost:8090
  CLAB_API_USERNAME     required
  CLAB_API_PASSWORD     required
  MCP_HOST / MCP_PORT   default 127.0.0.1 / 9000
  CLAB_WORKSPACE        default /home/cathal/agent_wd
"""

import json
import logging
import os
import re
import sys
import time
from typing import Optional

import anyio
import requests
from fastmcp import FastMCP
from netmiko import ConnectHandler

from topology_validator import TopologyError, validate_topology

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("clab-mcp")

API_SERVER_URL = os.getenv("CLAB_API_SERVER_URL", "http://localhost:8090")
API_USERNAME = os.getenv("CLAB_API_USERNAME")
API_PASSWORD = os.getenv("CLAB_API_PASSWORD")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "9000"))

WORKSPACE = os.path.realpath(os.getenv("CLAB_WORKSPACE", "/home/cathal/agent_wd"))
TOPO_NAME_RE = re.compile(r"^name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9_-]{0,62})['\"]?\s*(?:#.*)?$", re.M)

if not API_USERNAME or not API_PASSWORD:
    sys.exit("CLAB_API_USERNAME and CLAB_API_PASSWORD must be set")

LAB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# Commands execCommand may run. Each must match one pattern in full.
ARGS = r"[A-Za-z0-9 ./:_,=@%+-]*"
QUOTED = r'"[^"`$\\]+"'
EXEC_ALLOWLIST = [re.compile(p) for p in (
    # any ip command, except ones that run other programs (netns/vrf exec, batch files)
    rf"ip(?!.*\bexec\b)(?!.*(?:^| )-b(?:atch)?(?: |$))( {ARGS})?",
    # ping/ping6 must have a count, so they always finish
    rf"ping6?(?=.* -c ?[0-9]+(?: |$)) {ARGS}",
    # traceroute/traceroute6 with any arguments
    rf"traceroute6? {ARGS}",
    # mtr must run in report mode (the interactive UI would never return)
    rf"mtr(?=.* (?:-r|--report|-w|--report-wide)(?: |$)) {ARGS}",
    # any vtysh command(s), one or more -c "...", but no shell escape
    rf"vtysh(?!.*start-shell)(?: -c {QUOTED})+",
    # any IOS XR CLI command
    r'/pkg/bin/xr_cli "[^"`$\\]+"',
    # any Arista EOS CLI command (cEOS), privilege level 15
    r'Cli( -p ?15)? -c "[^"`$\\]+"',
    # any Junos CLI command (cRPD)
    r'cli -c "[^"`$\\]+"',
    # SR Linux (read-only): show and info commands, optionally "| as json/table/text/yaml"
    r'sr_cli "(?:show|info|tree) [A-Za-z0-9 ./:_,=@%+*\[\]-]+(?: \| as (?:json|table|text|yaml))?"',
)]

# ---- configuration and SSH -----------------------------------------------

# Native container kinds: configured through the API's exec, no SSH.
EXEC_CONFIG_KINDS = {
    "linux": "frr",
    "arista_ceos": "eos",
    "ceos": "eos",
    "cisco_xrd": "xr",
    "juniper_crpd": "crpd",
}

# vrnetlab (VM-based) kinds: exec can't reach their CLI, so they use SSH to
# the management IP. Containerlab's default credentials; if a startup config
# changes them, update the entry here.
VRNETLAB_KINDS = {
    "juniper_vjunosrouter":  {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
    "juniper_vjunosswitch":  {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
    "juniper_vjunosevolved": {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
    "juniper_vmx":           {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
    "juniper_vqfx":          {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
    "juniper_vsrx":          {"device_type": "juniper_junos", "username": "admin", "password": "admin@123"},
}

# Characters a configuration line may not contain: they would break the
# quoting of the exec command, or (for cRPD) split commands.
CONFIG_LINE_FORBIDDEN = set('"`$\\\'\n\r;')
MAX_CONFIG_LINES = 500
MAX_OUTPUT = 60_000


class ClabApiClient:
    """HTTP client for the ContainerLab API server with auto-authentication."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self.session = requests.Session()
        self.session.trust_env = False  # don't send local API traffic via a proxy

    def _ensure_auth(self):
        if self.token and time.time() < self.token_expiry:
            return
        resp = self.session.post(
            f"{self.base_url}/login",
            json={"username": self.username, "password": self.password},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Authentication failed (HTTP {resp.status_code})")
        self.token = resp.json().get("token")
        if not self.token:
            raise RuntimeError("No token in login response")
        self.token_expiry = time.time() + 50 * 60

    def request(self, method: str, path: str, json_body=None, params=None):
        self._ensure_auth()
        return self.session.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=json_body,
            params=params,
            timeout=120,
        )


client = ClabApiClient(API_SERVER_URL, API_USERNAME, API_PASSWORD)
mcp = FastMCP("ContainerLab MCP Server")


def _check_lab_name(name: str) -> Optional[str]:
    if not LAB_NAME_RE.match(name or ""):
        return "ERROR: invalid lab name (letters, digits, '-' and '_' only)"
    return None


def _result(resp, action: str) -> str:
    if resp.status_code not in (200, 201):
        return f"ERROR: Failed to {action} (HTTP {resp.status_code}): {resp.text}"
    try:
        return json.dumps(resp.json(), indent=2)
    except ValueError:
        return resp.text


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [output truncated, {len(text) - MAX_OUTPUT} more characters]"


def _node_info(lab: str, node: str) -> dict:
    """Look up a running node's kind and management address via the API."""
    if not LAB_NAME_RE.match(lab or "") or not LAB_NAME_RE.match(node or ""):
        raise ValueError("invalid lab or node name")
    resp = client.request("GET", f"/api/v1/labs/{lab}")
    if resp.status_code != 200:
        raise ValueError(f"cannot inspect lab {lab!r} (HTTP {resp.status_code}): {resp.text}")
    for c in resp.json():
        if c.get("nodeName") == node:
            break
    else:
        raise ValueError(f"no node {node!r} in lab {lab!r}")
    if c.get("state") != "running":
        raise ValueError(f"node {node!r} is {c.get('state')!r}, not running")
    addr = (c.get("ipv4_address") or c.get("ipv6_address") or "").split("/")[0]
    return {"kind": c.get("kind"), "host": addr}


def _exec(lab: str, node: str, command: str) -> str:
    resp = client.request("POST", f"/api/v1/labs/{lab}/exec",
                          json_body={"command": command}, params={"nodes": node})
    return _result(resp, "execute command")


def _ssh_params(info: dict) -> dict:
    creds = VRNETLAB_KINDS[info["kind"]]
    if not info["host"] or info["host"] == "N/A":
        raise ValueError("node has no management address")
    return {**creds, "host": info["host"], "conn_timeout": 20,
            "auth_timeout": 30, "banner_timeout": 30}


def _ssh_commands(params: dict, commands: list) -> str:
    out = []
    with ConnectHandler(**params) as conn:
        for cmd in commands:
            result = conn.send_command(cmd, read_timeout=120)
            out.append(f"### {cmd}\n{result.rstrip()}")
    return "\n\n".join(out)


def _ssh_configure(params: dict, lines: list, comment: str) -> str:
    """Apply and commit config on a Junos node; roll back if the commit fails."""
    with ConnectHandler(**params) as conn:
        out = conn.send_config_set(lines, exit_config_mode=False)
        try:
            out += "\n" + conn.commit(comment=comment)
        except ValueError as e:  # netmiko raises when the commit fails
            out += f"\nCOMMIT FAILED: {e}\n" + conn.send_command_timing("rollback 0")
            out += conn.exit_config_mode()
            return out + "\n[changes discarded]"
        out += conn.exit_config_mode()
        return out


def _exec_config_command(method: str, lines: list) -> str:
    """Build the exec command that applies `lines` on a native container kind."""
    if method == "frr":
        parts = ['-c "configure terminal"'] + [f'-c "{l}"' for l in lines] + ['-c "end"']
        return "vtysh " + " ".join(parts)
    body = "\n".join(lines)
    if method == "eos":
        return f'Cli -p 15 -c "configure\n{body}\nend"'
    if method == "xr":
        # ZTP helper: applies the config and commits it
        return f"bash -c \"source /pkg/bin/ztp_helper.sh && xrapply_string '{body}'\""
    if method == "crpd":
        joined = "; ".join(lines)
        return f'cli -c "configure; {joined}; commit and-quit"'
    raise ValueError(f"unknown config method {method!r}")


@mcp.tool()
def listLabs() -> str:
    """List all labs you own on the ContainerLab server."""
    return _result(client.request("GET", "/api/v1/labs"), "list labs")


@mcp.tool()
def deployLab(topologyContent: dict, reconfigure: bool = False) -> str:
    """Deploy a new lab from a containerlab topology.

    topologyContent is a JSON object (not a string) with `name` and
    `topology` (`nodes`, `links`, optionally `kinds`), plus optional `mgmt`.
    Only approved kinds and images are accepted. Binds must be relative paths
    inside the lab directory, and exec may only configure addresses, bring
    links up or start services. If the topology is rejected, the error says
    which field to fix. Set reconfigure=true to redeploy an existing lab.
    """
    try:
        validate_topology(topologyContent)
    except TopologyError as e:
        return f"ERROR: Topology rejected: {e}"
    params = {"reconfigure": "true"} if reconfigure else None
    resp = client.request("POST", "/api/v1/labs",
                          json_body={"topologyContent": topologyContent}, params=params)
    return _result(resp, "deploy lab")


def _resolve_topology(topologyPath: str):
    if os.path.isabs(topologyPath) or not topologyPath.endswith((".yml", ".yaml")):
        raise ValueError("topologyPath must be a relative path to a .yml/.yaml file in the workspace")
    norm = os.path.normpath(topologyPath)
    if norm.startswith("..") or "/." in "/" + norm:
        raise ValueError("topologyPath must stay inside the workspace and not use hidden directories")
    full = os.path.join(WORKSPACE, norm)
    if os.path.islink(full) or os.path.commonpath([os.path.realpath(full), WORKSPACE]) != WORKSPACE:
        raise ValueError("topologyPath must be a regular file inside the workspace (no symlinks)")
    if not os.path.isfile(full):
        raise ValueError(f"no such topology file: {norm}")
    with open(full, encoding="utf-8") as f:
        m = TOPO_NAME_RE.search(f.read(1_000_000))
    if not m:
        raise ValueError("could not find a top-level 'name:' in the topology file")
    return norm, m.group(1)


@mcp.tool()
def deployLabFile(topologyPath: str, reconfigure: bool = False) -> str:
    """Deploy a lab from a topology file already in the workspace.

    topologyPath is relative to the workspace (the agent's working directory),
    e.g. "repos/v6mpls/sr-mpls-v6-xrd/sr-mpls-v6.yaml". The lab runs from that
    file's directory, so startup configs and bind paths resolve relative to
    it, and no copy of the topology is written. The lab name is taken from
    the file's top-level `name:`. Set reconfigure=true to redeploy.
    """
    try:
        path, lab = _resolve_topology(topologyPath)
    except (ValueError, OSError) as e:
        return f"ERROR: {e}"
    params = {"path": path}
    if reconfigure:
        params["reconfigure"] = "true"
    client._ensure_auth()
    resp = client.session.post(
        f"{client.base_url}/api/v1/labs/{lab}/deploy",
        headers={"Authorization": f"Bearer {client.token}"},
        params=params,
        timeout=900,
    )
    return _result(resp, f"deploy lab {lab!r} from {path}")


@mcp.tool()
def inspectLab(labName: str, details: bool = False) -> str:
    """Show a lab's nodes, their kinds, state and management IPs."""
    if err := _check_lab_name(labName):
        return err
    params = {"details": "true"} if details else None
    return _result(client.request("GET", f"/api/v1/labs/{labName}", params=params), "inspect lab")


@mcp.tool()
def execCommand(labName: str, command: str, nodeName: Optional[str] = None) -> str:
    """Run a command on one node, or all nodes, of a lab, inside its container.

    Works for native container kinds (Linux/FRR, cEOS, XRd, cRPD, SR-Linux). For
    VM-based vrnetlab kinds (vJunos, vMX, vQFX, vSRX) use sshCommand instead:
    their CLI isn't reachable from inside the container.
    nodeName is the node's name from the topology (e.g. "ce1", "pe1");
    omit it to run on every node.

    Allowed commands:
      Linux nodes: any `ip ...` (except netns/vrf exec and batch mode),
        `ping -c N ...`, `traceroute ...`, `mtr -r ...`
      FRR: `vtysh -c "<command>" [-c "<command>" ...]`
      IOS XR (XRd): `/pkg/bin/xr_cli "<command>"`
      Arista EOS (cEOS): `Cli -p 15 -c "<command>"`
      Junos (cRPD): `cli -c "<command>"`
      SR Linux: `sr_cli "show ..."` or `sr_cli "info [flat] from state|running <path>"`,
        optionally ending in `| as json` (read-only)

    Quoted commands must not contain double quotes, backticks, $ or
    backslashes. Anything else is refused. To change configuration, use
    configureNode.
    """
    if err := _check_lab_name(labName):
        return err
    if nodeName is not None and not LAB_NAME_RE.match(nodeName):
        return "ERROR: invalid node name"
    command = " ".join(command.split())
    if not any(p.fullmatch(command) for p in EXEC_ALLOWLIST):
        return f"ERROR: command not allowed: {command!r}"
    # `nodes` takes topology node names; `nodeFilter` would need full container names
    params = {"nodes": nodeName} if nodeName else None
    resp = client.request("POST", f"/api/v1/labs/{labName}/exec",
                          json_body={"command": command}, params=params)
    return _result(resp, "execute command")


@mcp.tool()
async def sshCommand(labName: str, nodeName: str, commands: list[str]) -> str:
    """Run CLI commands over SSH on a VM-based (vrnetlab) node, e.g. vJunos,
    vMX, vQFX or vSRX, and return their output.

    commands is a list of operational commands in the node's CLI syntax,
    e.g. ["show bgp summary", "show isis adjacency"]. Paging is disabled
    automatically. For native container kinds use execCommand; for
    configuration changes use configureNode.
    """
    if not commands:
        return "ERROR: no commands given"
    try:
        info = await anyio.to_thread.run_sync(_node_info, labName, nodeName)
        if info["kind"] not in VRNETLAB_KINDS:
            return (f"ERROR: {nodeName!r} is kind {info['kind']!r}; sshCommand is only for "
                    f"vrnetlab kinds ({', '.join(sorted(VRNETLAB_KINDS))}). Use execCommand.")
        out = await anyio.to_thread.run_sync(_ssh_commands, _ssh_params(info), commands)
    except Exception as e:  # report lookup/connection/auth errors to the model
        return f"ERROR: {type(e).__name__}: {e}"
    return _truncate(out)


@mcp.tool()
async def configureNode(labName: str, nodeName: str, configLines: list[str],
                        comment: str = "") -> str:
    """Apply configuration to a running node and return the device's output.

    configLines are configuration-mode lines in the node's own syntax,
    without the command that enters configuration mode and without commit:
      FRR (linux):  e.g. ["router bgp 64497", "neighbor 198.51.100.1 shutdown"]
      cEOS:         e.g. ["interface Ethernet1", "description to-ce1"]
      XRd:          e.g. ["interface GigabitEthernet0/0/0/0", "description to-ce1"]
      cRPD/Junos:   set commands, e.g. ["set interfaces eth1 description to-ce1"]
    XRd and Junos changes are committed (Junos with the optional comment; a
    failed Junos commit over SSH is rolled back). FRR and EOS lines take
    effect as entered. Check the output for errors: a rejected line is
    reported by the device, not by this tool. Lines may not contain double
    or single quotes, backticks, $, backslashes or semicolons.
    Changes are lost when the lab is redeployed; save the running config
    into the lab's startup_configs to keep them.
    """
    if not configLines:
        return "ERROR: no configuration lines given"
    if len(configLines) > MAX_CONFIG_LINES:
        return f"ERROR: at most {MAX_CONFIG_LINES} lines per call"
    lines = [l.strip() for l in configLines if l.strip()]
    for l in lines:
        if any(c in CONFIG_LINE_FORBIDDEN for c in l):
            return f"ERROR: line contains a forbidden character: {l!r}"
    try:
        info = await anyio.to_thread.run_sync(_node_info, labName, nodeName)
        kind = info["kind"]
        if kind in VRNETLAB_KINDS:
            if VRNETLAB_KINDS[kind]["device_type"] != "juniper_junos":
                return f"ERROR: configuration over SSH not implemented for {kind!r}"
            out = await anyio.to_thread.run_sync(_ssh_configure, _ssh_params(info), lines, comment)
            return _truncate(out)
        if kind in EXEC_CONFIG_KINDS:
            command = _exec_config_command(EXEC_CONFIG_KINDS[kind], lines)
            out = await anyio.to_thread.run_sync(_exec, labName, nodeName, command)
            return _truncate(out)
        return f"ERROR: no configuration method for kind {kind!r}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


@mcp.tool()
def destroyLab(labName: str, cleanup: bool = False, graceful: bool = False) -> str:
    """Destroy a lab. graceful=true shuts nodes down cleanly; cleanup=true
    also removes the lab directory."""
    if err := _check_lab_name(labName):
        return err
    params = {}
    if cleanup:
        params["cleanup"] = "true"
    if graceful:
        params["graceful"] = "true"
    return _result(client.request("DELETE", f"/api/v1/labs/{labName}", params=params or None),
                   "destroy lab")


if __name__ == "__main__":
    log.info("ContainerLab MCP server on http://%s:%d/mcp, API %s", MCP_HOST, MCP_PORT, API_SERVER_URL)
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT, show_banner=False)
