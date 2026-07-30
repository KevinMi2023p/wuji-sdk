#!/usr/bin/env python3
"""Quick Wuji Hand 2 motion demo.

Source: https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/
"""

import argparse
import ipaddress
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from wuji_sdk import DeviceType, JointCommand, SdkManager

DOC_URL = "https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/"
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


@dataclass(frozen=True)
class InterfaceAddress:
    name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


@dataclass(frozen=True)
class InstalledRoute:
    destination: ipaddress.IPv4Address
    interface: str


class TemporaryHandRoutes:
    """Install and later remove Linux host routes needed for direct-attached hands."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.ip_command = shutil.which("ip")
        self.ping_command = shutil.which("ping")
        self.installed: list[InstalledRoute] = []

    def configure(self, devices) -> None:
        if not self.enabled or sys.platform != "linux":
            return
        if self.ip_command is None or self.ping_command is None:
            return

        interfaces = self._interface_addresses()
        route_plans: list[tuple[ipaddress.IPv4Address, InterfaceAddress]] = []

        for device in devices:
            destination = parse_device_ipv4(device.address)
            if destination is None:
                continue

            candidates = [
                interface
                for interface in interfaces
                if destination in interface.network
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
            existing = self._exact_host_routes(destination)
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
                "replace",
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
            result = self._run_privileged(
                "route",
                "del",
                f"{route.destination}/32",
                "dev",
                route.interface,
                check=False,
            )
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

    def _run_privileged(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        command = [self.ip_command, *arguments]
        if os.geteuid() != 0:
            sudo_command = shutil.which("sudo")
            if sudo_command is None:
                raise RuntimeError(
                    "Temporary hand routes require root privileges, but sudo "
                    "was not found. Configure the routes manually or run with "
                    "--no-auto-routes."
                )
            command.insert(0, sudo_command)
        try:
            return subprocess.run(command, check=check)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "Failed to configure temporary hand routes. Configure the "
                "routes manually or run with --no-auto-routes."
            ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-auto-routes",
        action="store_true",
        help=(
            "Do not detect and temporarily fix duplicate-subnet routes for "
            "hands connected through separate Ethernet interfaces."
        ),
    )
    return parser.parse_args()


def parse_device_ipv4(address) -> ipaddress.IPv4Address | None:
    text = str(address)
    host = text.rsplit(":", 1)[0]
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    return parsed if isinstance(parsed, ipaddress.IPv4Address) else None


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


def main() -> int:
    args = parse_args()
    manager = SdkManager.instance()
    hands = []
    publishers = []
    hands_to_disable = []
    temporary_routes = TemporaryHandRoutes(enabled=not args.no_auto_routes)

    try:
        print(f"Using wuji-sdk {installed_sdk_version()}")
        devices = [
            device
            for device in manager.scan()
            if device.device_type == DeviceType.WujiHand2
        ]
        if not devices:
            print("No Wuji Hand 2 found; nothing to run.")
            return 0

        temporary_routes.configure(devices)

        found_sides = set()
        for index, device in enumerate(devices):
            hand = manager.connect(
                sn=device.sn,
                device_name=f"wuji_hand_2_{index}",
            )
            handedness = hand.handedness().get()
            side = str(handedness).lower()
            if side not in {"left", "right"}:
                raise RuntimeError(
                    f"{hand.serial_number} reported unknown handedness {handedness!r}"
                )
            found_sides.add(side)
            hands.append((hand, side))
            print(
                f"Connected to {hand.serial_number}: "
                f"{side}, {hand.online_joints_count().get()} joints online"
            )

        for side in ("left", "right"):
            if side not in found_sides:
                print(f"No {side} hand found; skipping it.")

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
        for publisher in reversed(publishers):
            try:
                publisher.close()
            except Exception as exc:
                print(f"Publisher close failed: {exc}", file=sys.stderr)
        for hand in reversed(hands_to_disable):
            try:
                hand.disable()
            except Exception as exc:
                print(f"Disable failed: {exc}", file=sys.stderr)
                if is_api_compatibility_error(exc):
                    print_compatibility_hint()
        try:
            manager.disconnect_all()
        finally:
            temporary_routes.restore()


if __name__ == "__main__":
    raise SystemExit(main())
