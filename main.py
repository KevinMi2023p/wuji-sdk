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
DEMO_SECONDS = 8.0
EFFORT_LIMIT_A = 1.2
KP = 2.0
KD = 0.05
SIDE = Handedness.Right

# Firmware order is finger-major: thumb, index, middle, ring, pinky; four
# joints per finger. These values keep the right hand mostly in positive curl
# from neutral and use a smaller, delayed thumb motion for opposition.
RIGHT_HAND_TARGETS_RAD = (
    (0.14, 0.08, 0.24, 0.16),  # thumb
    (0.42, 0.03, 0.58, 0.42),  # index
    (0.46, 0.02, 0.62, 0.46),  # middle
    (0.40, 0.02, 0.56, 0.40),  # ring
    (0.34, 0.02, 0.48, 0.34),  # pinky
)


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


def cycle_amount(t: float, period_s: float = 4.0) -> float:
    phase = (t % period_s) / period_s
    if phase < 0.5:
        return ease_in_out(phase * 2.0)
    return ease_in_out((1.0 - phase) * 2.0)


def delayed_thumb_amount(curl: float) -> float:
    """Start thumb opposition after the fingers are visibly curling."""
    return ease_in_out((curl - 0.25) / 0.75)


def make_right_hand_frame(t: float) -> list[JointCommand]:
    """Return one right-hand-optimized 20-joint command frame."""
    curl = cycle_amount(t)
    thumb_curl = delayed_thumb_amount(curl)
    commands = []

    for joint_index in range(TOTAL_JOINTS):
        finger = joint_index // JOINTS_PER_FINGER
        joint_in_finger = joint_index % JOINTS_PER_FINGER
        amount = thumb_curl if finger == 0 else curl
        position = RIGHT_HAND_TARGETS_RAD[finger][joint_in_finger] * amount
        commands.append(JointCommand(position, 0.0, 0.0))

    return commands


def main() -> int:
    manager = SdkManager.instance()
    hand = None
    publisher = None
    enabled = False

    try:
        print(f"Using wuji-sdk {installed_sdk_version()}")
        hand = manager.connect(handedness=SIDE, device_name="wuji_hand_2")
        handedness = hand.handedness().get()
        print(
            f"Connected to {hand.serial_number}: "
            f"{handedness}, {hand.online_joints_count().get()} joints online"
        )
        if str(handedness).lower() != "right":
            print(f"Expected a right hand, got {handedness!r}. Aborting.", file=sys.stderr)
            return 1

        hand.effort_limit().set(EFFORT_LIMIT_A)
        hand.mit_params().set((KP, KD))

        # Open the publisher before enabling motors. If this fails with a schema
        # mismatch, the firmware is incompatible and no motion has been enabled.
        publisher = hand.joint_command().publish()

        hand.enable()
        enabled = True
        time.sleep(0.8)

        print("Moving right hand through a gentle grasp cycle. Keep it clear; Ctrl+C stops.")

        dt = 1.0 / PUB_HZ
        started = time.monotonic()
        frame = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= DEMO_SECONDS:
                break

            publisher.send(make_right_hand_frame(elapsed))
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
