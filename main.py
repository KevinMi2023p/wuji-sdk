#!/usr/bin/env python3
"""Quick Wuji Hand 2 motion demo.

Source: https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/
"""

import math
import sys
import time
from importlib.metadata import PackageNotFoundError, version

from wuji_sdk import Handedness, JointCommand, SdkManager

DOC_URL = "https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/"
TOTAL_JOINTS = 20
JOINTS_PER_FINGER = 4
PUB_HZ = 100
DEMO_SECONDS = 6.0
EFFORT_LIMIT_A = 1.2
KP = 2.0
KD = 0.05
SIDE = Handedness.Right  # Change to Handedness.Left for a left hand.


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


def make_wave_frame(t: float) -> list[JointCommand]:
    """Return one 20-joint command frame for a gentle curling wave."""
    omega = 2.0 * math.pi / 2.5
    ramp = min(t / 1.0, 1.0)
    commands = []

    for joint_index in range(TOTAL_JOINTS):
        finger = joint_index // JOINTS_PER_FINGER
        joint_in_finger = joint_index % JOINTS_PER_FINGER
        phase = finger * 0.35
        amp = 0.18 if joint_in_finger == 1 else 0.35
        position = amp * math.sin(omega * t + phase) * ramp
        velocity = amp * omega * math.cos(omega * t + phase) * ramp
        commands.append(JointCommand(position, velocity, 0.0))

    return commands


def main() -> int:
    manager = SdkManager.instance()
    hand = None
    publisher = None
    enabled = False

    try:
        print(f"Using wuji-sdk {installed_sdk_version()}")
        hand = manager.connect(handedness=SIDE, device_name="wuji_hand_2")
        print(
            f"Connected to {hand.serial_number}: "
            f"{hand.handedness().get()}, {hand.online_joints_count().get()} joints online"
        )

        hand.effort_limit().set(EFFORT_LIMIT_A)
        hand.mit_params().set((KP, KD))

        # Open the publisher before enabling motors. If this fails with a schema
        # mismatch, the firmware is incompatible and no motion has been enabled.
        publisher = hand.joint_command().publish()

        hand.enable()
        enabled = True
        time.sleep(0.8)

        print("Moving hand with a gentle wave. Keep it clear; Ctrl+C stops.")

        dt = 1.0 / PUB_HZ
        started = time.monotonic()
        frame = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= DEMO_SECONDS:
                break

            publisher.send(make_wave_frame(elapsed))
            frame += 1

            wait = started + frame * dt - time.monotonic()
            if wait > 0:
                time.sleep(wait)

        zeros = [JointCommand(0.0, 0.0, 0.0) for _ in range(TOTAL_JOINTS)]
        for _ in range(PUB_HZ):
            publisher.send(zeros)
            time.sleep(dt)

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
        if publisher is not None:
            publisher.close()
        if hand is not None and enabled:
            try:
                hand.disable()
            except Exception as exc:
                print(f"Disable failed: {exc}", file=sys.stderr)
                if is_api_compatibility_error(exc):
                    print_compatibility_hint()
        manager.disconnect_all()


if __name__ == "__main__":
    raise SystemExit(main())
