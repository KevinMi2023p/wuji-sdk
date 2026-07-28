#!/usr/bin/env python3
"""Quick Wuji Hand 2 motion demo.

Source: https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/
"""

import math
import sys
import time
from importlib.metadata import PackageNotFoundError, version

from wuji_sdk import DeviceType, Handedness, JointCommand, SdkManager

DOC_URL = "https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/"
TOTAL_JOINTS = 20
JOINTS_PER_FINGER = 4
PUB_HZ = 150
DEMO_SECONDS = 5.0
GESTURE_SECONDS = 1.4
RETURN_SECONDS = 0.6
STATE_TIMEOUT_SECONDS = 2.0
ENABLE_TIMEOUT_SECONDS = 5.0
EFFORT_LIMIT_A = 1.4
KP = 2.6
KD = 0.05
SIDE = Handedness.Right

# Firmware order is finger-major: thumb, index, middle, ring, pinky; four
# joints per finger. These values keep the right hand mostly in positive curl
# from neutral and use a smaller, delayed thumb motion for opposition.
RIGHT_HAND_TARGETS_RAD = (
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
LEFT_HAND_OK_POSE_RAD = (
    (0.68, -0.49, 0.73, 0.51),  # thumb opposed to index
    (0.70, -0.03, 1.07, 0.74),  # index curled to thumb
    (0.0, 0.0, 0.0, 0.0),       # middle extended
    (0.0, 0.0, 0.0, 0.0),       # ring extended
    (0.0, 0.0, 0.0, 0.0),       # pinky extended
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


def make_right_hand_pose(t: float) -> tuple[tuple[float, ...], ...]:
    """Return one right-hand-optimized pose."""
    curl = cycle_amount(t)
    thumb_curl = delayed_thumb_amount(curl)
    fingers = []

    for finger, targets in enumerate(RIGHT_HAND_TARGETS_RAD):
        amount = thumb_curl if finger == 0 else curl
        fingers.append(tuple(position * amount for position in targets))

    return tuple(fingers)


def make_right_hand_frame(t: float) -> list[JointCommand]:
    """Return one right-hand-optimized 20-joint command frame."""
    return pose_to_commands(make_right_hand_pose(t))


def stream_pose(
    publisher,
    pose: tuple[tuple[float, ...], ...],
    seconds: float,
    dt: float,
) -> None:
    for _ in range(max(1, round(seconds * PUB_HZ))):
        publisher.send(pose_to_commands(pose))
        time.sleep(dt)


def transition_pose(
    publisher,
    start: tuple[tuple[float, ...], ...],
    end: tuple[tuple[float, ...], ...],
    seconds: float,
    dt: float,
) -> None:
    steps = max(1, round(seconds * PUB_HZ))
    for step in range(steps):
        amount = (step + 1) / steps
        publisher.send(pose_to_commands(blend_pose(start, end, amount)))
        time.sleep(dt)


def read_current_pose(
    hand,
    timeout_s: float = STATE_TIMEOUT_SECONDS,
) -> tuple[tuple[float, ...], ...]:
    subscription = hand.joint_states().subscribe()
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = subscription.recv()
            if frame is not None and len(frame.joints) == TOTAL_JOINTS:
                joints = sorted(frame.joints, key=lambda joint: joint.nid)
                if len({joint.nid for joint in joints}) == TOTAL_JOINTS:
                    positions = [joint.position for joint in joints]
                    return tuple(
                        tuple(positions[start : start + JOINTS_PER_FINGER])
                        for start in range(0, TOTAL_JOINTS, JOINTS_PER_FINGER)
                    )
            time.sleep(0.01)
        raise RuntimeError(
            f"Timed out waiting for all {TOTAL_JOINTS} left-hand joint positions"
        )
    finally:
        subscription.close()


def wait_until_enabled(
    hand,
    timeout_s: float = ENABLE_TIMEOUT_SECONDS,
) -> None:
    subscription = hand.joint_diagnostics().subscribe()
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = subscription.recv()
            if frame is not None and len(frame.joints) == TOTAL_JOINTS:
                node_ids = {joint.nid for joint in frame.joints}
                if len(node_ids) == TOTAL_JOINTS and all(
                    joint.status_word.ext_state == 2 for joint in frame.joints
                ):
                    return
            time.sleep(0.05)
        raise RuntimeError(
            f"Timed out waiting for all {TOTAL_JOINTS} left-hand joints to enable"
        )
    finally:
        subscription.close()


def perform_left_hand_ok_sign(manager, dt: float) -> None:
    left_devices = [
        device
        for device in manager.scan()
        if device.device_type == DeviceType.WujiHand2
        and len(device.sn) > 3
        and device.sn[3].upper() == "J"
    ]
    if not left_devices:
        print("No left Wuji Hand 2 found; skipping the final OK sign.")
        return
    if len(left_devices) > 1:
        serial_numbers = ", ".join(sorted(device.sn for device in left_devices))
        print(
            f"Multiple left Wuji Hand 2 devices found ({serial_numbers}); "
            "skipping the final OK sign.",
            file=sys.stderr,
        )
        return

    left_hand = None
    left_publisher = None
    left_enable_attempted = False

    try:
        left_hand = manager.connect(
            sn=left_devices[0].sn,
            device_name="wuji_hand_2_left",
        )
        handedness = left_hand.handedness().get()
        online_joints = left_hand.online_joints_count().get()
        print(
            f"Connected to {left_hand.serial_number}: "
            f"{handedness}, {online_joints} joints online"
        )
        if str(handedness).lower() != "left":
            raise RuntimeError(f"Expected a left hand, got {handedness!r}")
        if online_joints != TOTAL_JOINTS:
            raise RuntimeError(
                f"Expected {TOTAL_JOINTS} online left-hand joints, got {online_joints}"
            )

        left_hand.effort_limit().set(EFFORT_LIMIT_A)
        left_hand.mit_params().set((KP, KD))
        left_publisher = left_hand.joint_command().publish()

        left_enable_attempted = True
        left_hand.enable()
        wait_until_enabled(left_hand)
        current_pose = read_current_pose(left_hand)

        print("Finishing with a left-hand OK sign. Keep it clear; Ctrl+C stops.")
        transition_pose(
            left_publisher,
            current_pose,
            LEFT_HAND_OK_POSE_RAD,
            1.0,
            dt,
        )
        stream_pose(left_publisher, LEFT_HAND_OK_POSE_RAD, GESTURE_SECONDS, dt)
        transition_pose(
            left_publisher,
            LEFT_HAND_OK_POSE_RAD,
            NEUTRAL_POSE_RAD,
            RETURN_SECONDS,
            dt,
        )
        stream_pose(left_publisher, NEUTRAL_POSE_RAD, 0.4, dt)
    finally:
        try:
            if left_publisher is not None:
                left_publisher.close()
        finally:
            if left_hand is not None and left_enable_attempted:
                try:
                    left_hand.disable()
                except Exception as exc:
                    print(f"Left-hand disable failed: {exc}", file=sys.stderr)
                    if is_api_compatibility_error(exc):
                        print_compatibility_hint()


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

        final_cycle_pose = make_right_hand_pose(time.monotonic() - started)
        print("Finishing with right-hand middle finger pose.")
        transition_pose(publisher, final_cycle_pose, MIDDLE_FINGER_POSE_RAD, 1.0, dt)
        stream_pose(publisher, MIDDLE_FINGER_POSE_RAD, GESTURE_SECONDS, dt)
        transition_pose(publisher, MIDDLE_FINGER_POSE_RAD, NEUTRAL_POSE_RAD, RETURN_SECONDS, dt)
        stream_pose(publisher, NEUTRAL_POSE_RAD, 0.4, dt)

        right_publisher = publisher
        publisher = None
        right_publisher.close()
        hand.disable()
        enabled = False

        perform_left_hand_ok_sign(manager, dt)

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
            if publisher is not None:
                publisher.close()
        finally:
            try:
                if hand is not None and enabled:
                    try:
                        hand.disable()
                    except Exception as exc:
                        print(f"Disable failed: {exc}", file=sys.stderr)
                        if is_api_compatibility_error(exc):
                            print_compatibility_hint()
            finally:
                manager.disconnect_all()


if __name__ == "__main__":
    raise SystemExit(main())
