/*
 * C++ version of demo.py.
 *
 * This uses the C++-compatible API from the released Wuji C SDK
 * (wuji_sdk.h + libwuji_sdk_c.so).
 *
 * WARNING: This program moves every physical Wuji Hand 2 it finds. Keep the
 * hands clear while it is running. Press Ctrl+C to stop and disable the motors.
 */

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cctype>
#include <cstdint>
#include <exception>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include "wuji_sdk.h"

namespace {

using Clock = std::chrono::steady_clock;
using FingerPose = std::array<double, 4>;
using Pose = std::array<FingerPose, 5>;
using CommandFrame = std::array<WujiJointCommand, WUJI_HAND_2_JOINT_COUNT>;

constexpr char kDocUrl[] =
    "https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/";
constexpr std::size_t kTotalJoints = WUJI_HAND_2_JOINT_COUNT;
constexpr std::size_t kJointsPerFinger = 4;
constexpr int kPublishHz = 150;
constexpr double kDemoSeconds = 5.0;
constexpr double kGestureSeconds = 1.4;
constexpr double kReturnSeconds = 0.6;
constexpr float kEffortLimitAmps = 1.4F;
constexpr float kKp = 2.6F;
constexpr float kKd = 0.05F;
constexpr double kPi = 3.14159265358979323846;

static_assert(kTotalJoints == 5 * kJointsPerFinger,
              "The demo expects five fingers with four joints each");

// Firmware order is finger-major: thumb, index, middle, ring, pinky; four
// joints per finger. Both hands use positive curl from neutral. The thumb uses
// a smaller, delayed motion for opposition.
constexpr Pose kHandTargetsRad{{
    {{0.22, 0.12, 0.38, 0.25}},  // thumb
    {{0.70, 0.05, 0.95, 0.68}},  // index
    {{0.75, 0.04, 1.02, 0.74}},  // middle
    {{0.68, 0.04, 0.92, 0.66}},  // ring
    {{0.58, 0.04, 0.80, 0.58}},  // pinky
}};

constexpr Pose kNeutralPoseRad{{
    {{0.0, 0.0, 0.0, 0.0}},
    {{0.0, 0.0, 0.0, 0.0}},
    {{0.0, 0.0, 0.0, 0.0}},
    {{0.0, 0.0, 0.0, 0.0}},
    {{0.0, 0.0, 0.0, 0.0}},
}};

constexpr Pose kMiddleFingerPoseRad{{
    {{0.38, 0.20, 0.62, 0.44}},  // thumb tucked
    {{1.05, 0.05, 1.25, 0.92}},  // index curled
    {{0.0, 0.0, 0.0, 0.0}},      // middle extended
    {{1.00, 0.05, 1.22, 0.90}},  // ring curled
    {{0.90, 0.05, 1.10, 0.82}},  // pinky curled
}};

volatile std::sig_atomic_t gStopRequested = 0;

void on_sigint(int /*signal*/) {
    gStopRequested = 1;
}

struct Interrupted final {};

void throw_if_interrupted() {
    if (gStopRequested != 0) {
        throw Interrupted{};
    }
}

std::string last_sdk_error(WujiStatus status, std::string_view operation) {
    // wuji_last_error() is thread-local. Copy it before making another SDK call.
    const char* detail = wuji_last_error();
    std::string message(operation);
    message += ": ";
    if (detail != nullptr && detail[0] != '\0') {
        message += detail;
    } else {
        message += "SDK error ";
        message += std::to_string(static_cast<int>(status));
    }
    return message;
}

void check_status(WujiStatus status, std::string_view operation) {
    if (status != WUJI_STATUS_OK) {
        throw std::runtime_error(last_sdk_error(status, operation));
    }
}

bool is_api_compatibility_error(const std::string& message) {
    std::string lower = message;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return lower.find("schema mismatch") != std::string::npos ||
           lower.find("path not found") != std::string::npos;
}

void print_compatibility_hint() {
    std::cerr
        << "\nThis usually means the C SDK and hand firmware are from different "
           "Wuji Hand 2 API generations.\n"
        << "Update the hand firmware with Wuji Studio to the newest version "
           "offered for this hardware, or install the Wuji C SDK version that "
           "matches the firmware currently on the hand.\n"
        << "API source: " << kDocUrl << '\n';
}

class SdkSession {
public:
    SdkSession() {
        WujiInitOptions options{};
        options.log_level = 3;
        check_status(wuji_init(&options), "wuji_init");
        active_ = true;
    }

    ~SdkSession() {
        if (active_) {
            wuji_shutdown();
        }
    }

    SdkSession(const SdkSession&) = delete;
    SdkSession& operator=(const SdkSession&) = delete;

private:
    bool active_ = false;
};

struct DeviceDeleter {
    void operator()(WujiDevice* device) const noexcept {
        if (device == nullptr) {
            return;
        }

        const WujiStatus status = wuji_dev_disconnect(device);
        if (status != WUJI_STATUS_OK) {
            try {
                const std::string message =
                    last_sdk_error(status, "disconnect");
                std::cerr << message << '\n';
            } catch (...) {
                // Cleanup must continue through release even if logging fails.
            }
        }
        wuji_dev_release(device);
    }
};

using DeviceHandle = std::unique_ptr<WujiDevice, DeviceDeleter>;

class DiscoveredDevices {
public:
    DiscoveredDevices(WujiDiscovered* devices, std::size_t count)
        : devices_(devices), count_(count) {}

    ~DiscoveredDevices() {
        wuji_discovered_free(devices_, count_);
    }

    DiscoveredDevices(const DiscoveredDevices&) = delete;
    DiscoveredDevices& operator=(const DiscoveredDevices&) = delete;

    const WujiDiscovered& operator[](std::size_t index) const {
        return devices_[index];
    }

    std::size_t size() const {
        return count_;
    }

private:
    WujiDiscovered* devices_;
    std::size_t count_;
};

struct PublisherDeleter {
    void operator()(WujiJointCommandPublisher* publisher) const noexcept {
        wuji_joint_command_publisher_close(publisher);
    }
};

using PublisherHandle =
    std::unique_ptr<WujiJointCommandPublisher, PublisherDeleter>;

class MotorGuard {
public:
    explicit MotorGuard(WujiDevice* device) : device_(device) {}

    ~MotorGuard() {
        if (!disable_on_exit_) {
            return;
        }

        const WujiStatus status = wuji_hand_2_disable(device_, nullptr);
        if (status != WUJI_STATUS_OK) {
            try {
                const std::string message =
                    last_sdk_error(status, "Disable failed");
                std::cerr << message << '\n';
                if (is_api_compatibility_error(message)) {
                    print_compatibility_hint();
                }
            } catch (...) {
                // Never let cleanup logging prevent device disconnection.
            }
        }
    }

    void arm_for_enable_attempt() {
        // If enable reaches the hardware but its acknowledgement is lost, a
        // best-effort disable is still required while unwinding.
        disable_on_exit_ = true;
    }

    MotorGuard(const MotorGuard&) = delete;
    MotorGuard& operator=(const MotorGuard&) = delete;

private:
    WujiDevice* device_;
    bool disable_on_exit_ = false;
};

struct ConnectedHand {
    DeviceHandle device;
    std::string serial_number;
    WujiHandedness handedness;
};

std::vector<ConnectedHand> connect_hands() {
    WujiDiscovered* raw_devices = nullptr;
    std::size_t count = 0;
    const WujiStatus scan_status = wuji_scan(&raw_devices, &count);
    DiscoveredDevices devices(raw_devices, count);
    check_status(scan_status, "wuji_scan");
    throw_if_interrupted();

    std::vector<ConnectedHand> hands;
    for (std::size_t index = 0; index < devices.size(); ++index) {
        const WujiDiscovered& discovered = devices[index];
        if (discovered.device_id != WUJI_DEVICE_TYPE_WUJI_HAND_2) {
            continue;
        }

        WujiConnectTarget target{};
        target.kind = WUJI_CONNECT_TARGET_KIND_SN;
        target.value = discovered.serial_number;

        const std::string device_name =
            "wuji_hand_2_" + std::to_string(hands.size());
        WujiDevice* raw_device = nullptr;
        const WujiStatus connect_status =
            wuji_connect(
                &target, device_name.c_str(), nullptr, &raw_device);
        DeviceHandle device(raw_device);
        check_status(connect_status, "wuji_connect");
        throw_if_interrupted();

        WujiHandedness handedness = WUJI_HANDEDNESS_LEFT;
        check_status(
            wuji_hand_2_get_handedness(device.get(), &handedness),
            "handedness");
        throw_if_interrupted();

        hands.push_back({
            std::move(device),
            std::string(discovered.serial_number),
            handedness,
        });
    }
    return hands;
}

double ease_in_out(double value) {
    const double clamped = std::clamp(value, 0.0, 1.0);
    return 0.5 - 0.5 * std::cos(kPi * clamped);
}

double cycle_amount(double time_seconds, double period_seconds = 2.2) {
    const double phase = std::fmod(time_seconds, period_seconds) / period_seconds;
    if (phase < 0.5) {
        return ease_in_out(phase * 2.0);
    }
    return ease_in_out((1.0 - phase) * 2.0);
}

double delayed_thumb_amount(double curl) {
    return ease_in_out((curl - 0.25) / 0.75);
}

Pose make_hand_pose(double time_seconds) {
    const double curl = cycle_amount(time_seconds);
    const double thumb_curl = delayed_thumb_amount(curl);
    Pose pose{};

    for (std::size_t finger = 0; finger < pose.size(); ++finger) {
        const double amount = finger == 0 ? thumb_curl : curl;
        for (std::size_t joint = 0; joint < pose[finger].size(); ++joint) {
            pose[finger][joint] =
                kHandTargetsRad[finger][joint] * amount;
        }
    }
    return pose;
}

Pose blend_pose(const Pose& start, const Pose& end, double amount) {
    const double eased = ease_in_out(amount);
    Pose pose{};
    for (std::size_t finger = 0; finger < pose.size(); ++finger) {
        for (std::size_t joint = 0; joint < pose[finger].size(); ++joint) {
            const double from = start[finger][joint];
            const double to = end[finger][joint];
            pose[finger][joint] = from + (to - from) * eased;
        }
    }
    return pose;
}

CommandFrame pose_to_commands(const Pose& pose) {
    CommandFrame commands{};
    std::size_t command_index = 0;
    for (const FingerPose& finger : pose) {
        for (const double position : finger) {
            commands[command_index++] = {
                static_cast<float>(position),
                0.0F,
                0.0F,
            };
        }
    }
    return commands;
}

void send_pose(WujiJointCommandPublisher* publisher, const Pose& pose) {
    const CommandFrame commands = pose_to_commands(pose);
    check_status(
        wuji_joint_command_publisher_send(publisher, commands.data()),
        "publisher.send");
}

void send_pose(const std::vector<PublisherHandle>& publishers,
               const Pose& pose) {
    for (const PublisherHandle& publisher : publishers) {
        send_pose(publisher.get(), pose);
    }
}

void sleep_until_interruptible(Clock::time_point deadline) {
    constexpr auto kMaxSleep = std::chrono::milliseconds(10);
    while (true) {
        throw_if_interrupted();
        const Clock::time_point now = Clock::now();
        if (now >= deadline) {
            return;
        }

        const Clock::duration remaining = deadline - now;
        const Clock::duration max_sleep =
            std::chrono::duration_cast<Clock::duration>(kMaxSleep);
        std::this_thread::sleep_for(std::min(remaining, max_sleep));
    }
}

void sleep_for_interruptible(double seconds) {
    sleep_until_interruptible(
        Clock::now() + std::chrono::duration_cast<Clock::duration>(
                           std::chrono::duration<double>(seconds)));
}

void stream_pose(const std::vector<PublisherHandle>& publishers,
                 const Pose& pose,
                 double seconds,
                 double frame_seconds) {
    const int steps =
        std::max(1, static_cast<int>(std::lround(seconds * kPublishHz)));
    for (int step = 0; step < steps; ++step) {
        throw_if_interrupted();
        send_pose(publishers, pose);
        sleep_for_interruptible(frame_seconds);
    }
}

void transition_pose(const std::vector<PublisherHandle>& publishers,
                     const Pose& start,
                     const Pose& end,
                     double seconds,
                     double frame_seconds) {
    const int steps =
        std::max(1, static_cast<int>(std::lround(seconds * kPublishHz)));
    for (int step = 0; step < steps; ++step) {
        throw_if_interrupted();
        const double amount =
            static_cast<double>(step + 1) / static_cast<double>(steps);
        send_pose(publishers, blend_pose(start, end, amount));
        sleep_for_interruptible(frame_seconds);
    }
}

int run_demo() {
    SdkSession sdk;
    std::cout << "Using wuji-sdk " << wuji_version() << '\n';

    std::vector<ConnectedHand> hands = connect_hands();
    throw_if_interrupted();

    if (hands.empty()) {
        std::cout << "No Wuji Hand 2 found; nothing to run.\n";
        return 0;
    }

    bool found_left = false;
    bool found_right = false;
    for (const ConnectedHand& hand : hands) {
        std::uint8_t online_joints = 0;
        check_status(
            wuji_hand_2_online_joints_count(
                hand.device.get(), &online_joints),
            "online_joints_count");
        throw_if_interrupted();

        const char* handedness_name =
            hand.handedness == WUJI_HANDEDNESS_RIGHT ? "right" : "left";
        found_right |= hand.handedness == WUJI_HANDEDNESS_RIGHT;
        found_left |= hand.handedness == WUJI_HANDEDNESS_LEFT;
        std::cout << "Connected to " << hand.serial_number << ": "
                  << handedness_name << ", "
                  << static_cast<unsigned int>(online_joints)
                  << " joints online\n";
    }
    if (!found_left) {
        std::cout << "No left hand found; skipping it.\n";
    }
    if (!found_right) {
        std::cout << "No right hand found; skipping it.\n";
    }

    std::array<float, kTotalJoints> kp{};
    std::array<float, kTotalJoints> kd{};
    kp.fill(kKp);
    kd.fill(kKd);

    // Declare the motor guards before the publishers so destruction happens
    // in the required order: close publishers, disable motors, disconnect.
    std::vector<std::unique_ptr<MotorGuard>> motors;
    std::vector<PublisherHandle> publishers;
    motors.reserve(hands.size());
    publishers.reserve(hands.size());

    for (ConnectedHand& hand : hands) {
        check_status(
            wuji_hand_2_set_all_effort_limit(
                hand.device.get(), kEffortLimitAmps),
            "effort_limit");
        throw_if_interrupted();
        check_status(
            wuji_hand_2_set_all_mit_params(
                hand.device.get(), kp.data(), kd.data()),
            "mit_params");
        throw_if_interrupted();

        motors.push_back(std::make_unique<MotorGuard>(hand.device.get()));

        // Open every publisher before enabling any motors. If this fails
        // because of a schema mismatch, no motion has been enabled.
        WujiJointCommandPublisher* raw_publisher = nullptr;
        const WujiStatus publish_status =
            wuji_hand_2_joint_command_publish(
                hand.device.get(), &raw_publisher);
        PublisherHandle publisher(raw_publisher);
        check_status(publish_status, "open publisher");
        throw_if_interrupted();
        publishers.push_back(std::move(publisher));
    }

    for (std::size_t index = 0; index < hands.size(); ++index) {
        motors[index]->arm_for_enable_attempt();
        check_status(
            wuji_hand_2_enable(hands[index].device.get(), nullptr),
            "enable");
        throw_if_interrupted();
    }
    sleep_for_interruptible(0.8);

    std::cout
        << "Moving all connected hands through a gentle grasp cycle. "
           "Keep them clear; Ctrl+C stops.\n";

    constexpr double frame_seconds =
        1.0 / static_cast<double>(kPublishHz);
    const Clock::time_point started = Clock::now();
    std::uint64_t frame = 0;

    while (true) {
        throw_if_interrupted();
        const double elapsed =
            std::chrono::duration<double>(Clock::now() - started).count();
        if (elapsed >= kDemoSeconds) {
            break;
        }

        send_pose(publishers, make_hand_pose(elapsed));
        ++frame;
        const auto target_offset =
            std::chrono::duration_cast<Clock::duration>(
                std::chrono::duration<double>(
                    static_cast<double>(frame) * frame_seconds));
        sleep_until_interruptible(started + target_offset);
    }

    const double final_elapsed =
        std::chrono::duration<double>(Clock::now() - started).count();
    const Pose final_cycle_pose = make_hand_pose(final_elapsed);

    std::cout << "Finishing with a middle finger pose.\n";
    transition_pose(
        publishers,
        final_cycle_pose,
        kMiddleFingerPoseRad,
        1.0,
        frame_seconds);
    stream_pose(
        publishers,
        kMiddleFingerPoseRad,
        kGestureSeconds,
        frame_seconds);
    transition_pose(
        publishers,
        kMiddleFingerPoseRad,
        kNeutralPoseRad,
        kReturnSeconds,
        frame_seconds);
    stream_pose(
        publishers,
        kNeutralPoseRad,
        0.4,
        frame_seconds);

    std::cout << "Demo complete.\n";
    return 0;
}

}  // namespace

int main() {
    std::signal(SIGINT, on_sigint);

    try {
        return run_demo();
    } catch (const Interrupted&) {
        std::cout << "\nStopped.\n";
        return 130;
    } catch (const std::exception& error) {
        const std::string message = error.what();
        std::cerr << "Demo failed: " << message << '\n';
        if (is_api_compatibility_error(message)) {
            print_compatibility_hint();
        }
        return 1;
    }
}
