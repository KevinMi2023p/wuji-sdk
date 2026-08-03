import ipaddress
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import demo


HAND_IP = ipaddress.IPv4Address("192.168.1.111")
HAND_INTERFACE = demo.InterfaceAddress(
    name="eth2",
    address=ipaddress.IPv4Address("192.168.1.50"),
    network=ipaddress.IPv4Network("192.168.1.0/24"),
)


def hand_device(
    address="192.168.1.111:7447",
    sn="WH2KA00260603001",
):
    return SimpleNamespace(
        sn=sn,
        device_type=demo.DeviceType.WujiHand2,
        address=address,
    )


class WslConfigTests(unittest.TestCase):
    def test_missing_wsl2_section_is_appended_without_losing_settings(self):
        contents = "[experimental]\nautoMemoryReclaim=gradual\n"

        updated, changed = demo.configure_mirrored_wslconfig(contents)

        self.assertTrue(changed)
        self.assertIn(contents, updated)
        self.assertIn("[wsl2]\nnetworkingMode=mirrored\n", updated)

    def test_existing_networking_mode_is_replaced_and_comments_survive(self):
        contents = (
            "[wsl2]\r\n"
            "memory=8GB\r\n"
            "networkingMode = NAT ; keep this note\r\n"
            "[experimental]\r\n"
            "sparseVhd=true\r\n"
        )

        updated, changed = demo.configure_mirrored_wslconfig(contents)

        self.assertTrue(changed)
        self.assertIn(
            "networkingMode = mirrored ; keep this note\r\n", updated
        )
        self.assertIn("memory=8GB\r\n", updated)
        self.assertIn("[experimental]\r\nsparseVhd=true\r\n", updated)

    def test_existing_mirrored_mode_is_unchanged(self):
        contents = "[wsl2]\nnetworkingMode=mirrored\n"

        updated, changed = demo.configure_mirrored_wslconfig(contents)

        self.assertFalse(changed)
        self.assertEqual(updated, contents)

    def test_preflight_persists_setting_and_requests_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".wslconfig"
            config_path.write_text("[wsl2]\nnetworkingMode=nat\n")

            with (
                mock.patch.object(demo, "is_wsl", return_value=True),
                self.assertRaisesRegex(
                    demo.NetworkConfigurationError, "wsl --shutdown"
                ),
            ):
                demo.ensure_wsl_mirrored_networking(config_path)

            self.assertEqual(
                config_path.read_text(),
                "[wsl2]\nnetworkingMode=mirrored\n",
            )

    def test_preflight_rejects_configured_but_not_applied_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".wslconfig"
            config_path.write_text("[wsl2]\nnetworkingMode=mirrored\n")

            with (
                mock.patch.object(demo, "is_wsl", return_value=True),
                mock.patch.object(
                    demo, "current_wsl_networking_mode", return_value="nat"
                ),
                self.assertRaisesRegex(
                    demo.NetworkConfigurationError, "still using 'nat'"
                ),
            ):
                demo.ensure_wsl_mirrored_networking(config_path)


class ScanManager:
    def __init__(self, responses):
        self.responses = list(responses)
        self.scans = 0

    def scan(self):
        self.scans += 1
        return self.responses.pop(0)


class FakeRoutes:
    def __init__(self, reachable=(HAND_IP,), enabled=True):
        self.enabled = enabled
        self.reachable = tuple(reachable)
        self.prepare_calls = []
        self.configure_calls = []
        self.restores = 0

    def prepare_discovery(self, candidates, *, strict):
        self.prepare_calls.append((tuple(candidates), strict))
        return self.reachable

    def configure(self, devices):
        self.configure_calls.append(list(devices))

    def restore(self):
        self.restores += 1


class DiscoveryTests(unittest.TestCase):
    def test_complete_normal_scan_never_touches_routes(self):
        left = hand_device(
            address="192.168.1.110:7447",
            sn="WH2KA-LEFT",
        )
        device = hand_device()
        manager = ScanManager([[left, device]])
        routes = FakeRoutes()

        result = demo.discover_hand_devices(
            manager, routes, running_in_wsl=True
        )

        self.assertEqual(result, [left, device])
        self.assertEqual(manager.scans, 1)
        self.assertEqual(routes.prepare_calls, [])

    def test_partial_normal_scan_probes_and_merges_missing_factory_hand(self):
        left_ip, right_ip = demo.FACTORY_HAND_IPS
        left = hand_device(
            address=f"{left_ip}:7447",
            sn="WH2KA-LEFT",
        )
        right = hand_device(
            address=f"{right_ip}:7447",
            sn="WH2KA-RIGHT",
        )
        manager = ScanManager([[left], [right]])
        routes = FakeRoutes(reachable=(right_ip,))

        result = demo.discover_hand_devices(
            manager, routes, running_in_wsl=True
        )

        self.assertEqual(result, [left, right])
        self.assertEqual(routes.prepare_calls, [((right_ip,), False)])
        self.assertEqual(routes.restores, 1)

    def test_empty_wsl_scan_probes_factory_ips_then_rescans(self):
        device = hand_device()
        manager = ScanManager([[], [device]])
        routes = FakeRoutes()

        result = demo.discover_hand_devices(
            manager, routes, running_in_wsl=True
        )

        self.assertEqual(result, [device])
        self.assertEqual(manager.scans, 2)
        self.assertEqual(
            routes.prepare_calls,
            [
                ((demo.FACTORY_HAND_IPS[0],), False),
                ((demo.FACTORY_HAND_IPS[1],), False),
            ],
        )

    def test_two_explicit_hands_are_discovered_on_separate_links(self):
        left_ip, right_ip = demo.FACTORY_HAND_IPS
        left = hand_device(
            address=f"{left_ip}:7447",
            sn="WH2KA-LEFT",
        )
        right = hand_device(
            address=f"{right_ip}:7447",
            sn="WH2KA-RIGHT",
        )
        manager = ScanManager([[], [left], [right]])
        routes = FakeRoutes(reachable=(left_ip, right_ip))

        result = demo.discover_hand_devices(
            manager,
            routes,
            hand_ips=(left_ip, right_ip),
            running_in_wsl=True,
        )

        self.assertEqual(result, [left, right])
        self.assertEqual(
            routes.prepare_calls,
            [((left_ip,), True), ((right_ip,), True)],
        )
        self.assertEqual(routes.restores, 2)

    def test_explicit_ip_is_strict_and_works_outside_wsl(self):
        custom = ipaddress.IPv4Address("192.168.2.42")
        device = hand_device(address="192.168.2.42:7447")
        manager = ScanManager([[], [device]])
        routes = FakeRoutes(reachable=(custom,))

        result = demo.discover_hand_devices(
            manager,
            routes,
            hand_ips=(custom,),
            running_in_wsl=False,
        )

        self.assertEqual(result, [device])
        self.assertEqual(routes.prepare_calls, [((custom,), True)])

    def test_explicit_ip_selects_only_the_requested_device(self):
        left = hand_device(
            address="192.168.1.110:7447",
            sn="WH2KA-LEFT",
        )
        right = hand_device()
        manager = ScanManager([[left, right]])
        routes = FakeRoutes()

        result = demo.discover_hand_devices(
            manager,
            routes,
            hand_ips=(HAND_IP,),
            running_in_wsl=True,
        )

        self.assertEqual(result, [right])
        self.assertEqual(manager.scans, 1)
        self.assertEqual(routes.prepare_calls, [])

    def test_same_sized_wrong_scan_repairs_and_requires_requested_ip(self):
        custom = ipaddress.IPv4Address("192.168.2.42")
        wrong = hand_device()
        requested = hand_device(address="192.168.2.42:7447")
        manager = ScanManager([[wrong], [requested]])
        routes = FakeRoutes(reachable=(custom,))

        result = demo.discover_hand_devices(
            manager,
            routes,
            hand_ips=(custom,),
            running_in_wsl=False,
        )

        self.assertEqual(result, [requested])
        self.assertEqual(manager.scans, 2)
        self.assertEqual(routes.prepare_calls, [((custom,), True)])

    def test_empty_rescan_reports_hyper_v_firewall_command(self):
        manager = ScanManager([[], []])
        routes = FakeRoutes()

        with self.assertRaisesRegex(
            demo.NetworkConfigurationError,
            "New-NetFirewallHyperVRule",
        ) as caught:
            demo.discover_hand_devices(
                manager, routes, running_in_wsl=True
            )

        self.assertIn("192.168.1.111", str(caught.exception))
        self.assertIn(demo.WSL_VM_CREATOR_ID, str(caught.exception))

    def test_missing_factory_hands_reports_wsl_nic_guidance(self):
        manager = ScanManager([[]])
        routes = FakeRoutes(reachable=())

        with self.assertRaisesRegex(
            demo.NetworkConfigurationError,
            "192.168.1.x/24",
        ):
            demo.discover_hand_devices(
                manager, routes, running_in_wsl=True
            )

    def test_disabled_routes_do_not_probe_or_rescan(self):
        manager = ScanManager([[]])
        routes = FakeRoutes(enabled=False)

        self.assertEqual(
            demo.discover_hand_devices(
                manager, routes, running_in_wsl=True
            ),
            [],
        )
        self.assertEqual(manager.scans, 1)
        self.assertEqual(routes.prepare_calls, [])


class RouteTests(unittest.TestCase):
    def test_exact_host_route_makes_32_interface_eligible_for_discovery(self):
        routes = demo.TemporaryHandRoutes()
        routes.ip_command = "/usr/sbin/ip"
        routes.ping_command = "/usr/bin/ping"
        host_route_interface = demo.InterfaceAddress(
            name="eth4",
            address=ipaddress.IPv4Address("192.168.1.51"),
            network=ipaddress.IPv4Network("192.168.1.51/32"),
        )

        with (
            mock.patch.object(
                routes,
                "_interface_addresses",
                return_value=[host_route_interface],
            ),
            mock.patch.object(
                routes,
                "_exact_host_routes",
                return_value=[{"dev": "eth4"}],
            ),
            mock.patch.object(
                routes,
                "_is_reachable",
                side_effect=lambda _destination, interface: interface == "eth4",
            ),
            mock.patch.object(
                routes, "_broadcast_egress_interface", return_value="eth4"
            ),
        ):
            reachable = routes.prepare_discovery((HAND_IP,), strict=True)

        self.assertEqual(reachable, (HAND_IP,))

    def test_factory_probe_routes_only_the_reachable_hand_and_restores(self):
        routes = demo.TemporaryHandRoutes()
        routes.ip_command = "/usr/sbin/ip"
        routes.ping_command = "/usr/bin/ping"
        commands = []

        with (
            mock.patch.object(
                routes, "_interface_addresses", return_value=[HAND_INTERFACE]
            ),
            mock.patch.object(
                routes,
                "_is_reachable",
                side_effect=lambda destination, _interface: destination == HAND_IP,
            ),
            mock.patch.object(
                routes, "_broadcast_egress_interface", return_value="eth3"
            ),
            mock.patch.object(routes, "_local_broadcast_routes", return_value=[]),
            mock.patch.object(routes, "_exact_host_routes", return_value=[]),
            mock.patch.object(
                routes,
                "_run_privileged",
                side_effect=lambda *args, **_kwargs: (
                    commands.append(args) or SimpleNamespace(returncode=0)
                ),
            ),
        ):
            reachable = routes.prepare_discovery(
                demo.FACTORY_HAND_IPS, strict=False
            )
            routes.restore()

        self.assertEqual(reachable, (HAND_IP,))
        self.assertEqual(
            commands[0],
            (
                "route",
                "add",
                "table",
                "local",
                "broadcast",
                "255.255.255.255",
                "dev",
                "eth2",
                "src",
                "192.168.1.50",
            ),
        )
        self.assertEqual(
            commands[1],
            (
                "route",
                "del",
                "table",
                "local",
                "broadcast",
                "255.255.255.255",
                "dev",
                "eth2",
            ),
        )
        self.assertIsNone(routes.installed_broadcast)

    def test_post_scan_duplicate_subnet_host_route_is_preserved(self):
        routes = demo.TemporaryHandRoutes()
        routes.ip_command = "/usr/sbin/ip"
        routes.ping_command = "/usr/bin/ping"
        other_interface = demo.InterfaceAddress(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.1.97"),
            network=ipaddress.IPv4Network("192.168.1.0/24"),
        )
        commands = []

        with (
            mock.patch.object(
                routes,
                "_interface_addresses",
                return_value=[other_interface, HAND_INTERFACE],
            ),
            mock.patch.object(
                routes,
                "_is_reachable",
                side_effect=lambda _destination, interface: interface == "eth2",
            ),
            mock.patch.object(routes, "_exact_host_routes", return_value=[]),
            mock.patch.object(
                routes,
                "_run_privileged",
                side_effect=lambda *args, **_kwargs: (
                    commands.append(args) or SimpleNamespace(returncode=0)
                ),
            ),
        ):
            routes.configure([hand_device()])
            routes.restore()

        self.assertEqual(
            commands,
            [
                (
                    "route",
                    "add",
                    "192.168.1.111/32",
                    "dev",
                    "eth2",
                    "src",
                    "192.168.1.50",
                ),
                (
                    "route",
                    "del",
                    "192.168.1.111/32",
                    "dev",
                    "eth2",
                ),
            ],
        )
        self.assertEqual(routes.installed, [])


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class ReadOnlyHand:
    serial_number = "WH2KA00260603001"

    def __init__(self):
        self.actuation_calls = []

    def handedness(self):
        return Value("right")

    def online_joints_count(self):
        return Value(demo.TOTAL_JOINTS)

    def effort_limit(self):
        self.actuation_calls.append("effort_limit")
        raise AssertionError("read-only check configured motors")

    def mit_params(self):
        self.actuation_calls.append("mit_params")
        raise AssertionError("read-only check configured motors")

    def joint_command(self):
        self.actuation_calls.append("joint_command")
        raise AssertionError("read-only check opened a publisher")

    def enable(self):
        self.actuation_calls.append("enable")
        raise AssertionError("read-only check enabled motors")


class MainManager(ScanManager):
    def __init__(self, device, hand):
        super().__init__([[device]])
        self.hand = hand
        self.connects = 0
        self.connect_options = None
        self.disconnects = 0

    def connect(self, **kwargs):
        self.connects += 1
        self.connect_options = kwargs.get("options")
        return self.hand

    def disconnect_all(self):
        self.disconnects += 1


class MainSafetyTests(unittest.TestCase):
    def test_termination_signal_runs_cleanup_and_restores_handler(self):
        class TerminatingHand(ReadOnlyHand):
            def online_joints_count(self):
                handler = demo.signal.getsignal(demo.signal.SIGTERM)
                handler(demo.signal.SIGTERM, None)
                raise AssertionError("termination handler returned")

        device = hand_device()
        manager = MainManager(device, TerminatingHand())
        routes = FakeRoutes()
        previous_handler = demo.signal.getsignal(demo.signal.SIGTERM)

        class FakeSdkManager:
            @staticmethod
            def instance():
                return manager

        with (
            mock.patch.object(demo, "SdkManager", FakeSdkManager),
            mock.patch.object(
                demo, "TemporaryHandRoutes", return_value=routes
            ),
        ):
            result = demo.main([])

        self.assertEqual(result, 130)
        self.assertEqual(manager.disconnects, 1)
        self.assertEqual(routes.restores, 1)
        self.assertIs(
            demo.signal.getsignal(demo.signal.SIGTERM),
            previous_handler,
        )

    def test_disable_failure_requests_emergency_stop(self):
        class FailingDisableHand:
            def __init__(self):
                self.emergency_stops = 0

            def disable(self):
                raise RuntimeError("disable transport failed")

            def emergency_stop(self):
                self.emergency_stops += 1

        hand = FailingDisableHand()

        demo.disable_hand_safely(hand)

        self.assertEqual(hand.emergency_stops, 1)

    def test_default_mode_connects_but_never_actuates(self):
        device = hand_device()
        hand = ReadOnlyHand()
        manager = MainManager(device, hand)
        routes = FakeRoutes()

        class FakeSdkManager:
            @staticmethod
            def instance():
                return manager

        with (
            mock.patch.object(demo, "SdkManager", FakeSdkManager),
            mock.patch.object(
                demo, "TemporaryHandRoutes", return_value=routes
            ),
        ):
            result = demo.main([])

        self.assertEqual(result, 0)
        self.assertEqual(manager.connects, 1)
        self.assertIsNotNone(manager.connect_options)
        self.assertFalse(manager.connect_options.enable_bridge)
        self.assertEqual(manager.disconnects, 1)
        self.assertEqual(hand.actuation_calls, [])
        self.assertEqual(routes.restores, 1)

    def test_scan_only_never_connects(self):
        device = hand_device()
        hand = ReadOnlyHand()
        manager = MainManager(device, hand)
        routes = FakeRoutes()

        class FakeSdkManager:
            @staticmethod
            def instance():
                return manager

        with (
            mock.patch.object(demo, "SdkManager", FakeSdkManager),
            mock.patch.object(
                demo, "TemporaryHandRoutes", return_value=routes
            ),
        ):
            result = demo.main(["--scan-only"])

        self.assertEqual(result, 0)
        self.assertEqual(manager.connects, 0)
        self.assertEqual(manager.disconnects, 1)
        self.assertEqual(routes.restores, 1)

    def test_route_cleanup_runs_after_discovery_failure(self):
        manager = ScanManager([[], []])
        manager.disconnects = 0
        manager.disconnect_all = lambda: setattr(
            manager, "disconnects", manager.disconnects + 1
        )
        routes = FakeRoutes()

        class FakeSdkManager:
            @staticmethod
            def instance():
                return manager

        with (
            mock.patch.object(demo, "SdkManager", FakeSdkManager),
            mock.patch.object(
                demo, "TemporaryHandRoutes", return_value=routes
            ),
            mock.patch.object(demo, "is_wsl", return_value=True),
        ):
            result = demo.main([])

        self.assertEqual(result, 1)
        self.assertEqual(manager.disconnects, 1)
        self.assertEqual(routes.restores, 2)


if __name__ == "__main__":
    unittest.main()
