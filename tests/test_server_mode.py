import unittest

from src.config.server_mode import ServerMode


class ServerModeTests(unittest.TestCase):
    def test_defaults_preserve_standalone_upstream_behavior(self):
        self.assertEqual(ServerMode.from_environment({}), ServerMode("0.0.0.0", False))

    def test_gateway_can_bind_each_miner_to_loopback(self):
        self.assertEqual(ServerMode.from_environment({"TDM_HOST": "127.0.0.1"}).host, "127.0.0.1")

    def test_remote_login_flags(self):
        for flag in ("1", "true", "YES", " True "):
            with self.subTest(flag=flag):
                self.assertTrue(ServerMode.from_environment({"TDM_REMOTE_LOGIN": flag}).remote_login)
        for flag in ("", "0", "false", "no"):
            with self.subTest(flag=flag):
                self.assertFalse(ServerMode.from_environment({"TDM_REMOTE_LOGIN": flag}).remote_login)

    def test_empty_host_is_rejected_instead_of_exposing_a_tenant(self):
        with self.assertRaises(ValueError):
            ServerMode.from_environment({"TDM_HOST": " "})
