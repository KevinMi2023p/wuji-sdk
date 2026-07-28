#!/usr/bin/env python3
"""Quick Wuji Hand 2 motion demo.

Source: https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/
"""

import math
import sys
import time
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
    manager = SdkManager.instance()
    hands = []
    publishers = []
    hands_to_disable = []

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
        manager.disconnect_all()


if __name__ == "__main__":
    raise SystemExit(main())
