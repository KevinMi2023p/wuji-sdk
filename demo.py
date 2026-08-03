#!/usr/bin/env python3
"""Wuji Hand 2 connection check and opt-in motion demo.

Source: https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/
"""

import argparse
import contextlib
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence

from wuji_sdk import ConnectOptions, DeviceType, JointCommand, SdkManager

DOC_URL = "https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/"
FACTORY_HAND_IPS = (
    ipaddress.IPv4Address("192.168.1.110"),  # left
    ipaddress.IPv4Address("192.168.1.111"),  # right
)
DISCOVERY_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
WSL_VM_CREATOR_ID = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"
WSL2_SECTION_PATTERN = re.compile(
    r"^\s*\[\s*wsl2\s*\]\s*(?:[#;].*)?(?:\r?\n)?$", re.IGNORECASE
)
WSL_SECTION_PATTERN = re.compile(
    r"^\s*\[[^]]+\]\s*(?:[#;].*)?(?:\r?\n)?$"
)
WSL_NETWORKING_MODE_PATTERN = re.compile(
    r"^(?P<prefix>\s*networkingMode\s*=\s*)"
    r"(?P<value>[^#;\r\n]*?)"
    r"(?P<suffix>\s*(?:[#;].*)?)"
    r"(?P<ending>\r?\n)?$",
    re.IGNORECASE,
)
TOTAL_JOINTS = 20
JOINTS_PER_FINGER = 4
PUB_HZ = 150
DEMO_SECONDS = 5.0
GESTURE_SECONDS = 1.4
RETURN_SECONDS = 0.6
EFFORT_LIMIT_A = 1.4
KP = 2.6
KD = 0.05

# Firmware order is finger-major: thumb, index, middle, ring, pinky; four
# joints per finger. Both hands use positive curl from neutral. The thumb uses
# a smaller, delayed motion for opposition.
HAND_TARGETS_RAD = (
    (0.22, 0.12, 0.38, 0.25),  # thumb
    (0.70, 0.05, 0.95, 0.68),  # index
    (0.75, 0.04, 1.02, 0.74),  # middle
    (0.68, 0.04, 0.92, 0.66),  # ring
    (0.58, 0.04, 0.80, 0.58),  # pinky
)
NEUTRAL_POSE_RAD = (
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
)
MIDDLE_FINGER_POSE_RAD = (
    (0.38, 0.20, 0.62, 0.44),  # thumb tucked
    (1.05, 0.05, 1.25, 0.92),  # index curled
    (0.0, 0.0, 0.0, 0.0),      # middle extended
    (1.00, 0.05, 1.22, 0.90),  # ring curled
    (0.90, 0.05, 1.10, 0.82),  # pinky curled
)


class NetworkConfigurationError(RuntimeError):
    """The host network cannot currently discover a Wuji Hand 2."""


@dataclass(frozen=True)
class InterfaceAddress:
    name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


@dataclass(frozen=True)
class InstalledRoute:
    destination: ipaddress.IPv4Address
    interface: str


@dataclass(frozen=True)
class InstalledBroadcastRoute:
    interface: str


def is_wsl(
    environ: Mapping[str, str] | None = None,
    release: str | None = None,
) -> bool:
    """Return whether this process is running inside WSL."""

    selected_environ = os.environ if environ is None else environ
    selected_release = platform.release() if release is None else release
    return bool(
        selected_environ.get("WSL_INTEROP")
        or selected_environ.get("WSL_DISTRO_NAME")
        or "microsoft" in selected_release.lower()
    )


def configure_mirrored_wslconfig(contents: str) -> tuple[str, bool]:
    """Return .wslconfig contents with networkingMode set to mirrored."""

    newline = "\r\n" if "\r\n" in contents else "\n"
    lines = contents.splitlines(keepends=True)
    section_start = next(
        (
            index
            for index, line in enumerate(lines)
            if WSL2_SECTION_PATTERN.match(line)
        ),
        None,
    )

    if section_start is None:
        updated = contents
        if updated and not updated.endswith(("\n", "\r")):
            updated += newline
        if updated and not updated.endswith(newline * 2):
            updated += newline
        updated += f"[wsl2]{newline}networkingMode=mirrored{newline}"
        return updated, True

    section_end = next(
        (
            index
            for index in range(section_start + 1, len(lines))
            if WSL_SECTION_PATTERN.match(lines[index])
        ),
        len(lines),
    )
    setting_indices = []
    changed = False
    for index in range(section_start + 1, section_end):
        match = WSL_NETWORKING_MODE_PATTERN.match(lines[index])
        if match is None:
            continue
        setting_indices.append(index)
        replacement = (
            f"{match.group('prefix')}mirrored"
            f"{match.group('suffix')}{match.group('ending') or ''}"
        )
        if replacement != lines[index]:
            lines[index] = replacement
            changed = True

    if setting_indices:
        return "".join(lines), changed

    if section_end > 0 and not lines[section_end - 1].endswith(("\n", "\r")):
        lines[section_end - 1] += newline
    lines.insert(section_end, f"networkingMode=mirrored{newline}")
    return "".join(lines), True


def windows_wslconfig_path() -> Path:
    """Resolve the current Windows user's persistent WSL configuration."""

    powershell = shutil.which("powershell.exe")
    wslpath = shutil.which("wslpath")
    if powershell is None or wslpath is None:
        raise NetworkConfigurationError(
            "cannot locate powershell.exe or wslpath to verify mirrored WSL "
            "networking"
        )
    profile = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Environment]::GetFolderPath('UserProfile')",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not profile:
        raise NetworkConfigurationError(
            "Windows did not report a user profile path for .wslconfig"
        )
    linux_profile = subprocess.run(
        [wslpath, "-u", profile],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(linux_profile) / ".wslconfig"


def current_wsl_networking_mode() -> str | None:
    """Return the networking mode applied to this WSL VM when available."""

    wslinfo = shutil.which("wslinfo")
    if wslinfo is None:
        return None
    result = subprocess.run(
        [wslinfo, "--networking-mode"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().lower() or None


def ensure_wsl_mirrored_networking(
    config_path: Path | None = None,
) -> None:
    """Persist mirrored networking and require a restart when not yet active."""

    if not is_wsl():
        return
    selected_path = windows_wslconfig_path() if config_path is None else config_path
    try:
        contents = selected_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        contents = ""
    except OSError as exc:
        raise NetworkConfigurationError(
            f"could not read Windows WSL configuration {selected_path}: {exc}"
        ) from exc

    updated, changed = configure_mirrored_wslconfig(contents)
    if changed:
        try:
            selected_path.write_text(updated, encoding="utf-8", newline="")
        except OSError as exc:
            raise NetworkConfigurationError(
                f"could not update Windows WSL configuration {selected_path}: "
                f"{exc}"
            ) from exc
        raise NetworkConfigurationError(
            f"set networkingMode=mirrored permanently in {selected_path}. "
            "The setting takes effect after WSL restarts. From Windows "
            "PowerShell run `wsl --shutdown`, reopen WSL, then rerun this demo."
        )

    active_mode = current_wsl_networking_mode()
    if active_mode is not None and active_mode != "mirrored":
        raise NetworkConfigurationError(
            f"{selected_path} requests mirrored networking, but this WSL VM "
            f"is still using {active_mode!r}. From Windows PowerShell run "
            "`wsl --shutdown`, reopen WSL, then rerun this demo."
        )


def parse_hand_ipv4(value: str) -> ipaddress.IPv4Address:
    """Argparse converter for an exact Wuji hand IPv4 selector."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an IP address") from None
    if not isinstance(address, ipaddress.IPv4Address):
        raise argparse.ArgumentTypeError(f"{value!r} is not an IPv4 address")
    return address


class TemporaryHandRoutes:
    """Install and remove discovery and per-hand routes on multi-NIC Linux."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.ip_command = shutil.which("ip")
        self.ping_command = shutil.which("ping")
        self.installed: list[InstalledRoute] = []
        self.installed_broadcast: InstalledBroadcastRoute | None = None

    def prepare_discovery(
        self,
        candidates: Sequence[ipaddress.IPv4Address],
        *,
        strict: bool,
    ) -> tuple[ipaddress.IPv4Address, ...]:
        """Find reachable hand IPs and pin limited broadcast to their link.

        Explicit ``--hand-ip`` hints are strict: every address must be reachable.
        Factory defaults are probes, so an absent left or right hand is ignored.
        """

        if not self.enabled or sys.platform != "linux":
            return ()
        self._require_network_tools()

        interfaces = self._interface_addresses()
        reachable: dict[ipaddress.IPv4Address, InterfaceAddress] = {}
        for destination in candidates:
            exact_route_interfaces = {
                str(route.get("dev"))
                for route in self._exact_host_routes(destination)
                if route.get("dev") is not None
            }
            same_subnet = [
                interface
                for interface in interfaces
                if destination in interface.network
                or interface.name in exact_route_interfaces
            ]
            if not same_subnet:
                if strict:
                    raise NetworkConfigurationError(
                        f"no local interface shares a subnet with hand {destination}"
                    )
                continue

            by_name = {
                interface.name: interface
                for interface in same_subnet
                if self._is_reachable(destination, interface.name)
            }
            if not by_name:
                if strict:
                    names = ", ".join(sorted({item.name for item in same_subnet}))
                    raise NetworkConfigurationError(
                        f"hand {destination} did not reply through {names}; "
                        "check power, cabling, and --hand-ip"
                    )
                continue
            if len(by_name) > 1:
                raise NetworkConfigurationError(
                    f"hand {destination} replied through multiple interfaces "
                    f"({', '.join(sorted(by_name))}); configure an exact host route"
                )
            reachable[destination] = next(iter(by_name.values()))

        if not reachable:
            return ()

        selected_by_name = {item.name: item for item in reachable.values()}
        if len(selected_by_name) > 1:
            raise NetworkConfigurationError(
                "reachable hands use more than one interface "
                f"({', '.join(sorted(selected_by_name))}), but limited-broadcast "
                "discovery can be pinned to only one link; connect both hands "
                "through one switch or configure discovery manually"
            )
        selected = next(iter(selected_by_name.values()))

        if self._broadcast_egress_interface() == selected.name:
            return tuple(reachable)

        existing = self._local_broadcast_routes()
        if existing:
            current = ", ".join(
                sorted(
                    str(route.get("dev"))
                    for route in existing
                    if route.get("dev") is not None
                )
            )
            raise NetworkConfigurationError(
                f"an explicit route for {DISCOVERY_BROADCAST} already uses "
                f"{current or 'an unknown interface'}, but the hand is on "
                f"{selected.name}; fix or remove that route first"
            )

        print(
            "Discovery broadcast currently leaves through another interface; "
            f"temporarily routing it through {selected.name}.",
            file=sys.stderr,
        )
        self._run_privileged(
            "route",
            "add",
            "table",
            "local",
            "broadcast",
            str(DISCOVERY_BROADCAST),
            "dev",
            selected.name,
            "src",
            str(selected.address),
        )
        self.installed_broadcast = InstalledBroadcastRoute(interface=selected.name)
        return tuple(reachable)

    def configure(self, devices) -> None:
        if not self.enabled or sys.platform != "linux":
            return
        self._require_network_tools()

        interfaces = self._interface_addresses()
        route_plans: list[tuple[ipaddress.IPv4Address, InterfaceAddress]] = []

        for device in devices:
            destination = parse_device_ipv4(device.address)
            if destination is None:
                continue

            existing = self._exact_host_routes(destination)
            exact_route_interfaces = {
                str(route.get("dev"))
                for route in existing
                if route.get("dev") is not None
            }
            candidates = [
                interface
                for interface in interfaces
                if destination in interface.network
                or interface.name in exact_route_interfaces
            ]
            candidate_names = {candidate.name for candidate in candidates}
            if len(candidate_names) < 2:
                continue

            reachable = [
                candidate
                for candidate in candidates
                if self._is_reachable(destination, candidate.name)
            ]
            reachable_names = {candidate.name for candidate in reachable}
            if len(reachable_names) != 1:
                names = ", ".join(sorted(candidate_names))
                raise RuntimeError(
                    f"Cannot determine which interface reaches {destination}; "
                    f"probed {names} and got {len(reachable_names)} unique replies. "
                    "Connect both hands through one Ethernet switch, or configure "
                    "a /32 host route for each hand manually."
                )

            selected = next(
                candidate
                for candidate in reachable
                if candidate.name in reachable_names
            )
            if existing:
                existing_interfaces = {
                    str(route.get("dev"))
                    for route in existing
                    if route.get("dev") is not None
                }
                if selected.name not in existing_interfaces:
                    raise RuntimeError(
                        f"Existing host route for {destination} uses "
                        f"{', '.join(sorted(existing_interfaces)) or 'an unknown interface'}, "
                        f"but the hand replies on {selected.name}. Fix or remove the "
                        "existing route before running the demo."
                    )
                continue

            route_plans.append((destination, selected))

        if not route_plans:
            return

        print(
            "Multiple Ethernet interfaces share the hand subnet. "
            "Administrative access is needed for temporary per-hand routes."
        )
        for destination, interface in route_plans:
            self._run_privileged(
                "route",
                "add",
                f"{destination}/32",
                "dev",
                interface.name,
                "src",
                str(interface.address),
            )
            self.installed.append(
                InstalledRoute(
                    destination=destination,
                    interface=interface.name,
                )
            )
            print(f"Temporary route: {destination} via {interface.name}")

    def restore(self) -> None:
        for route in reversed(self.installed):
            try:
                result = self._run_privileged(
                    "route",
                    "del",
                    f"{route.destination}/32",
                    "dev",
                    route.interface,
                    check=False,
                )
            except Exception as exc:
                print(
                    f"Could not remove temporary route for {route.destination} "
                    f"({exc}); remove it manually with: sudo ip route del "
                    f"{route.destination}/32 dev {route.interface}",
                    file=sys.stderr,
                )
                continue
            if result.returncode == 0:
                print(f"Removed temporary route for {route.destination}")
            else:
                print(
                    f"Could not remove temporary route for {route.destination}; "
                    "remove it manually with: "
                    f"sudo ip route del {route.destination}/32 dev {route.interface}",
                    file=sys.stderr,
                )
        self.installed.clear()

        if self.installed_broadcast is None:
            return
        interface = self.installed_broadcast.interface
        try:
            result = self._run_privileged(
                "route",
                "del",
                "table",
                "local",
                "broadcast",
                str(DISCOVERY_BROADCAST),
                "dev",
                interface,
                check=False,
            )
        except Exception as exc:
            print(
                "Could not remove the temporary Wuji discovery route "
                f"({exc}); remove it manually with: sudo ip route del table "
                f"local broadcast {DISCOVERY_BROADCAST} dev {interface}",
                file=sys.stderr,
            )
            return
        if result.returncode == 0:
            print(f"Removed temporary discovery route via {interface}")
            self.installed_broadcast = None
        else:
            print(
                "Could not remove the temporary Wuji discovery route; remove "
                f"it manually with: sudo ip route del table local broadcast "
                f"{DISCOVERY_BROADCAST} dev {interface}",
                file=sys.stderr,
            )

    def _interface_addresses(self) -> list[InterfaceAddress]:
        result = subprocess.run(
            [self.ip_command, "-j", "-4", "address", "show", "up"],
            check=True,
            capture_output=True,
            text=True,
        )
        interfaces = []
        for link in json.loads(result.stdout):
            interface_name = link.get("ifname")
            if not interface_name:
                continue
            for address_info in link.get("addr_info", []):
                if (
                    address_info.get("family") != "inet"
                    or address_info.get("scope") != "global"
                ):
                    continue
                address = ipaddress.IPv4Address(address_info["local"])
                network = ipaddress.IPv4Network(
                    f"{address}/{address_info['prefixlen']}",
                    strict=False,
                )
                interfaces.append(
                    InterfaceAddress(
                        name=interface_name,
                        address=address,
                        network=network,
                    )
                )
        return interfaces

    def _is_reachable(
        self,
        destination: ipaddress.IPv4Address,
        interface: str,
    ) -> bool:
        result = subprocess.run(
            [
                self.ping_command,
                "-4",
                "-c",
                "1",
                "-W",
                "1",
                "-I",
                interface,
                str(destination),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def _broadcast_egress_interface(self) -> str | None:
        result = subprocess.run(
            [self.ip_command, "-j", "route", "get", str(DISCOVERY_BROADCAST)],
            check=True,
            capture_output=True,
            text=True,
        )
        routes = json.loads(result.stdout)
        if not routes:
            return None
        device = routes[0].get("dev")
        return None if device is None else str(device)

    def _local_broadcast_routes(self) -> list[dict[str, object]]:
        result = subprocess.run(
            [
                self.ip_command,
                "-j",
                "route",
                "show",
                "table",
                "local",
                "exact",
                f"{DISCOVERY_BROADCAST}/32",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def _exact_host_routes(
        self,
        destination: ipaddress.IPv4Address,
    ) -> list[dict]:
        result = subprocess.run(
            [
                self.ip_command,
                "-j",
                "route",
                "show",
                "exact",
                f"{destination}/32",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def _require_network_tools(self) -> None:
        if self.ip_command is None or self.ping_command is None:
            raise NetworkConfigurationError(
                "automatic Wuji routing requires the Linux ip and ping commands; "
                "install iproute2/iputils-ping or use --no-auto-routes"
            )

    def _run_privileged(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.ip_command, *arguments]
        if os.geteuid() != 0:
            sudo_command = shutil.which("sudo")
            if sudo_command is None:
                raise NetworkConfigurationError(
                    "temporary hand routes require root privileges, but sudo "
                    "was not found; configure routes manually or use "
                    "--no-auto-routes"
                )
            command.insert(0, sudo_command)
        try:
            return subprocess.run(command, check=check)
        except subprocess.CalledProcessError as exc:
            raise NetworkConfigurationError(
                "failed to configure temporary hand routes; configure them "
                "manually or use --no-auto-routes"
            ) from exc


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and check Wuji Hand 2 devices. Motor movement is disabled "
            "unless --enable-motors is passed explicitly."
        )
    )
    parser.add_argument(
        "--hand-ip",
        action="append",
        dest="hand_ips",
        type=parse_hand_ipv4,
        metavar="IPV4",
        help=(
            "require and select this exact hand IPv4 address; repeat for both "
            "hands. On WSL the factory addresses 192.168.1.110/.111 are "
            "probed automatically when no address is supplied"
        ),
    )
    parser.add_argument(
        "--no-auto-routes",
        action="store_true",
        help="do not install temporary discovery or per-hand routes",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="connect read-only and report device health (this is the default)",
    )
    mode.add_argument(
        "--scan-only",
        action="store_true",
        help="discover and print devices without connecting to them",
    )
    mode.add_argument(
        "--enable-motors",
        action="store_true",
        help="run the existing motorized gesture demo",
    )
    return parser


def parse_device_ipv4(address) -> ipaddress.IPv4Address | None:
    text = str(address)
    host = text.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    return parsed if isinstance(parsed, ipaddress.IPv4Address) else None


def scan_hand_devices(manager) -> list:
    """Scan once and retain only Wuji Hand 2 devices."""

    return [
        device
        for device in manager.scan()
        if device.device_type == DeviceType.WujiHand2
    ]


def wsl_firewall_guidance(
    hand_ips: Sequence[ipaddress.IPv4Address],
) -> str:
    remote_addresses = ",".join(str(address) for address in hand_ips)
    return (
        "WSL can reach the Wuji hand IP, but SDK discovery still received no "
        "reply after routing the broadcast to the hand link. WSL's Hyper-V "
        "firewall is probably blocking the hand's UDP discovery reply. Run "
        "this once from an elevated Windows PowerShell, then retry:\n\n"
        "New-NetFirewallHyperVRule "
        f"-VMCreatorId '{WSL_VM_CREATOR_ID}' "
        "-Name 'WujiHandDiscovery' "
        "-DisplayName 'Wuji hand discovery reply' -Direction Inbound "
        f"-Action Allow -Protocol UDP -RemoteAddresses {remote_addresses}"
    )


def discover_hand_devices(
    manager,
    routes: TemporaryHandRoutes,
    *,
    hand_ips: Sequence[ipaddress.IPv4Address] = (),
    running_in_wsl: bool | None = None,
) -> list:
    """Discover requested hands, repairing limited-broadcast routing if needed."""

    devices = scan_hand_devices(manager)
    explicit_hints = bool(hand_ips)
    detected_wsl = is_wsl() if running_in_wsl is None else running_in_wsl

    def select_requested(
        scanned: Sequence,
    ) -> tuple[list, list[ipaddress.IPv4Address]]:
        by_ip = {
            address: device
            for device in scanned
            if (address := parse_device_ipv4(device.address)) is not None
        }
        missing = [address for address in hand_ips if address not in by_ip]
        return [by_ip[address] for address in hand_ips if address in by_ip], missing

    missing: list[ipaddress.IPv4Address] = []
    if explicit_hints:
        selected, missing = select_requested(devices)
        if not missing:
            return selected
    elif devices:
        discovered_factory_ips = {
            address
            for device in devices
            if (address := parse_device_ipv4(device.address)) in FACTORY_HAND_IPS
        }
        if (
            not routes.enabled
            or not detected_wsl
            or discovered_factory_ips == set(FACTORY_HAND_IPS)
        ):
            return devices

    if not routes.enabled or (not detected_wsl and not explicit_hints):
        if explicit_hints:
            raise NetworkConfigurationError(
                "requested Wuji Hand 2 IPv4 addresses were not discovered: "
                + ", ".join(str(address) for address in missing)
            )
        return []

    if explicit_hints:
        candidates = tuple(hand_ips)
    else:
        discovered_ips = {
            address
            for device in devices
            if (address := parse_device_ipv4(device.address)) is not None
        }
        candidates = tuple(
            address for address in FACTORY_HAND_IPS if address not in discovered_ips
        )
        if not candidates:
            return devices
    reachable_list: list[ipaddress.IPv4Address] = []
    discovered_by_ip = {
        address: device
        for device in devices
        if (address := parse_device_ipv4(device.address)) is not None
    }

    # A limited-broadcast route can use only one interface. Directly connected
    # left and right hands commonly live on separate USB Ethernet adapters, so
    # route discovery through each reachable link in turn and retain the union
    # of the scan results. The /32 routes used for the actual connections are
    # independent of this temporary broadcast route.
    for candidate in candidates:
        link_reachable = routes.prepare_discovery(
            (candidate,), strict=explicit_hints
        )
        selected_reachable = tuple(
            address for address in link_reachable if address == candidate
        )
        if not selected_reachable:
            continue
        reachable_list.extend(selected_reachable)
        try:
            for device in scan_hand_devices(manager):
                address = parse_device_ipv4(device.address)
                if address is not None:
                    discovered_by_ip[address] = device
        finally:
            # Remove this link's broadcast route before selecting the next one.
            routes.restore()

    reachable = tuple(dict.fromkeys(reachable_list))
    devices = list(discovered_by_ip.values())
    if not reachable:
        if explicit_hints:
            raise NetworkConfigurationError(
                "could not reach requested Wuji Hand 2 IPv4 addresses: "
                + ", ".join(str(address) for address in hand_ips)
            )
        if devices:
            return devices
        if detected_wsl:
            raise NetworkConfigurationError(
                "WSL did not reach either factory Wuji Hand 2 address "
                "(left 192.168.1.110, right 192.168.1.111). Set the hand-link "
                "adapter to a 192.168.1.x/24 address, check power/cabling, or "
                "pass the changed device address with --hand-ip."
            )
        return []

    if explicit_hints:
        selected, missing = select_requested(devices)
        if missing:
            if detected_wsl and not devices:
                raise NetworkConfigurationError(wsl_firewall_guidance(reachable))
            raise NetworkConfigurationError(
                "requested Wuji Hand 2 IPv4 addresses were not discovered: "
                + ", ".join(str(address) for address in missing)
            )
        return selected
    if not devices and detected_wsl:
        raise NetworkConfigurationError(wsl_firewall_guidance(reachable))
    return devices


def installed_sdk_version() -> str:
    try:
        return version("wuji-sdk")
    except PackageNotFoundError:
        return "unknown"


def is_api_compatibility_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "schema mismatch" in message or "path not found" in message


def print_compatibility_hint() -> None:
    print(
        "\nThis usually means the Python SDK and hand firmware are from "
        "different Wuji Hand 2 API generations.",
        file=sys.stderr,
    )
    print(
        "Update the hand firmware with Wuji Studio to the newest version offered "
        "for this hardware, or install the wuji-sdk version that matches the "
        "firmware currently on the hand.",
        file=sys.stderr,
    )
    print(f"API source: {DOC_URL}", file=sys.stderr)


def _request_shutdown(signum: int, _frame: Any) -> None:
    """Turn service-manager termination into the normal cleanup path."""

    signal_name = signal.Signals(signum).name
    print(f"\nReceived {signal_name}; stopping safely.", file=sys.stderr)
    raise KeyboardInterrupt


@contextlib.contextmanager
def termination_signal_handlers():
    """Route SIGTERM and SIGHUP through ``run_demo`` cleanup when possible."""

    previous: dict[signal.Signals, Any] = {}
    for signal_name in ("SIGTERM", "SIGHUP"):
        selected = getattr(signal, signal_name, None)
        if selected is None:
            continue
        try:
            previous[selected] = signal.signal(selected, _request_shutdown)
        except ValueError:
            # Python only permits signal handlers in the main thread.
            continue
    try:
        yield
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


def disable_hand_safely(hand) -> None:
    """Disable a hand, falling back to emergency stop if that fails."""

    try:
        hand.disable()
    except Exception as exc:
        print(f"Disable failed: {exc}; requesting emergency stop", file=sys.stderr)
        if is_api_compatibility_error(exc):
            print_compatibility_hint()
        try:
            hand.emergency_stop()
        except Exception as emergency_exc:
            print(f"Emergency stop failed: {emergency_exc}", file=sys.stderr)


def ease_in_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def cycle_amount(t: float, period_s: float = 2.2) -> float:
    phase = (t % period_s) / period_s
    if phase < 0.5:
        return ease_in_out(phase * 2.0)
    return ease_in_out((1.0 - phase) * 2.0)


def delayed_thumb_amount(curl: float) -> float:
    """Start thumb opposition after the fingers are visibly curling."""
    return ease_in_out((curl - 0.25) / 0.75)


def pose_to_commands(pose: tuple[tuple[float, ...], ...]) -> list[JointCommand]:
    return [JointCommand(position, 0.0, 0.0) for finger in pose for position in finger]


def blend_pose(
    start: tuple[tuple[float, ...], ...],
    end: tuple[tuple[float, ...], ...],
    amount: float,
) -> tuple[tuple[float, ...], ...]:
    eased = ease_in_out(amount)
    return tuple(
        tuple(a + (b - a) * eased for a, b in zip(start_finger, end_finger))
        for start_finger, end_finger in zip(start, end)
    )


def make_hand_pose(t: float) -> tuple[tuple[float, ...], ...]:
    """Return one grasp-cycle pose suitable for either hand."""
    curl = cycle_amount(t)
    thumb_curl = delayed_thumb_amount(curl)
    fingers = []

    for finger, targets in enumerate(HAND_TARGETS_RAD):
        amount = thumb_curl if finger == 0 else curl
        fingers.append(tuple(position * amount for position in targets))

    return tuple(fingers)


def stream_pose(
    publishers,
    pose: tuple[tuple[float, ...], ...],
    seconds: float,
    dt: float,
) -> None:
    for _ in range(max(1, round(seconds * PUB_HZ))):
        commands = pose_to_commands(pose)
        for publisher in publishers:
            publisher.send(commands)
        time.sleep(dt)


def transition_pose(
    publishers,
    start: tuple[tuple[float, ...], ...],
    end: tuple[tuple[float, ...], ...],
    seconds: float,
    dt: float,
) -> None:
    steps = max(1, round(seconds * PUB_HZ))
    for step in range(steps):
        amount = (step + 1) / steps
        commands = pose_to_commands(blend_pose(start, end, amount))
        for publisher in publishers:
            publisher.send(commands)
        time.sleep(dt)


def run_demo(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    hand_ips = tuple(args.hand_ips or ())
    if len(set(hand_ips)) != len(hand_ips):
        parser.error("--hand-ip values must be unique")

    manager = None
    hands = []
    publishers = []
    hands_to_disable = []
    temporary_routes = TemporaryHandRoutes(enabled=not args.no_auto_routes)

    try:
        ensure_wsl_mirrored_networking()
        manager = SdkManager.instance()
        print(f"Using wuji-sdk {installed_sdk_version()}")
        devices = discover_hand_devices(
            manager,
            temporary_routes,
            hand_ips=hand_ips,
        )
        if not devices:
            suffix = (
                " Automatic route repair was disabled."
                if args.no_auto_routes and is_wsl()
                else ""
            )
            print(f"No Wuji Hand 2 found.{suffix}", file=sys.stderr)
            return 1

        for device in devices:
            print(
                f"Discovered SN={device.sn}, type={device.device_type}, "
                f"address={device.address}"
            )
        if args.scan_only:
            print("Scan complete; no hand connection or motor command was made.")
            return 0

        temporary_routes.configure(devices)

        found_sides = set()
        connect_options = ConnectOptions(enable_bridge=False)
        for index, device in enumerate(devices):
            hand = manager.connect(
                sn=device.sn,
                device_name=f"wuji_hand_2_{index}",
                options=connect_options,
            )
            handedness = hand.handedness().get()
            side = str(handedness).lower()
            if side not in {"left", "right"}:
                raise RuntimeError(
                    f"{hand.serial_number} reported unknown handedness {handedness!r}"
                )
            found_sides.add(side)
            hands.append((hand, side))
            online_joints = hand.online_joints_count().get()
            print(
                f"Connected to {hand.serial_number}: "
                f"{side}, {online_joints} joints online"
            )
            if online_joints != TOTAL_JOINTS:
                raise RuntimeError(
                    f"{hand.serial_number} has {online_joints}/{TOTAL_JOINTS} "
                    "joints online"
                )

        for side in ("left", "right"):
            if side not in found_sides:
                print(f"No {side} hand found; skipping it.")

        if not args.enable_motors:
            print(
                "Connection check complete; motors were not configured, enabled, "
                "or commanded."
            )
            return 0

        for hand, _side in hands:
            hand.effort_limit().set(EFFORT_LIMIT_A)
            hand.mit_params().set((KP, KD))

            # Open every publisher before enabling motors. If this fails with a
            # schema mismatch, no hand has been enabled yet.
            publishers.append(hand.joint_command().publish())

        for hand, _side in hands:
            # Include the hand before the call so cleanup still makes a
            # best-effort disable if enable reaches the hardware but its
            # acknowledgement is lost.
            hands_to_disable.append(hand)
            hand.enable()
        time.sleep(0.8)

        side_names = " and ".join(sorted(found_sides))
        hand_word = "hand" if len(hands) == 1 else "hands"
        pronoun = "it" if len(hands) == 1 else "them"
        print(
            f"Moving the {side_names} {hand_word} through a gentle grasp cycle. "
            f"Keep {pronoun} clear; Ctrl+C stops."
        )

        dt = 1.0 / PUB_HZ
        started = time.monotonic()
        frame = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= DEMO_SECONDS:
                break

            commands = pose_to_commands(make_hand_pose(elapsed))
            for publisher in publishers:
                publisher.send(commands)
            frame += 1

            wait = started + frame * dt - time.monotonic()
            if wait > 0:
                time.sleep(wait)

        final_cycle_pose = make_hand_pose(time.monotonic() - started)
        print("Finishing with a middle finger pose.")
        transition_pose(
            publishers, final_cycle_pose, MIDDLE_FINGER_POSE_RAD, 1.0, dt
        )
        stream_pose(publishers, MIDDLE_FINGER_POSE_RAD, GESTURE_SECONDS, dt)
        transition_pose(
            publishers,
            MIDDLE_FINGER_POSE_RAD,
            NEUTRAL_POSE_RAD,
            RETURN_SECONDS,
            dt,
        )
        stream_pose(publishers, NEUTRAL_POSE_RAD, 0.4, dt)

        print("Demo complete.")
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        if is_api_compatibility_error(exc):
            print_compatibility_hint()
        return 1
    finally:
        try:
            for publisher in reversed(publishers):
                try:
                    publisher.close()
                except Exception as exc:
                    print(f"Publisher close failed: {exc}", file=sys.stderr)
            for hand in reversed(hands_to_disable):
                disable_hand_safely(hand)
            if manager is not None:
                try:
                    manager.disconnect_all()
                except Exception as exc:
                    print(f"Disconnect failed: {exc}", file=sys.stderr)
        finally:
            temporary_routes.restore()


def main(argv: Sequence[str] | None = None) -> int:
    with termination_signal_handlers():
        return run_demo(argv)


if __name__ == "__main__":
    raise SystemExit(main())
