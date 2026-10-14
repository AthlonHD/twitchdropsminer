import importlib.util
import json
import pathlib
import unittest
import unittest.mock
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "tdm_login_helper", pathlib.Path(__file__).parent.parent / "scripts" / "tdm_login_helper.py"
)
helper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helper)


class TestFindBrowser(unittest.TestCase):
    def _installed(self, installed: dict):
        return patch.object(helper, "_candidates", side_effect=lambda name: installed.get(name, []))

    def test_falls_back_chrome_then_edge_then_chromium(self):
        with self._installed({"chromium": ["/usr/bin/chromium"]}) as cand:
            self.assertEqual(helper.find_browser(None), "/usr/bin/chromium")
        self.assertEqual([c.args[0] for c in cand.call_args_list], ["chrome", "edge", "chromium"])

    def test_prefers_chrome_when_available(self):
        with self._installed({"chrome": ["chrome.exe"], "edge": ["msedge.exe"]}):
            self.assertEqual(helper.find_browser(None), "chrome.exe")

    def test_explicit_browser_does_not_fall_back(self):
        with self._installed({"chrome": ["chrome.exe"]}) as cand:
            self.assertIsNone(helper.find_browser("edge"))
        self.assertEqual(cand.call_count, 1)

    def test_none_found_returns_none_with_a_helpful_message(self):
        with self._installed({}), patch("sys.stderr") as err:
            self.assertIsNone(helper.find_browser(None))
        self.assertTrue(err.write.called)


class TestWsFrame(unittest.TestCase):
    def test_masked_text_frame_round_trips(self):
        payload = b'{"id": 1, "method": "Storage.getCookies"}'
        frame = helper.ws_frame(payload)
        self.assertEqual(frame[0], 0x81)          # FIN + text
        self.assertTrue(frame[1] & 0x80)          # client frames are masked
        length = frame[1] & 0x7F
        mask = frame[2:6]
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:6 + length]))
        self.assertEqual(body, payload)

    def test_long_payload_uses_extended_length(self):
        frame = helper.ws_frame(b"x" * 300)
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(int.from_bytes(frame[2:4], "big"), 300)


class TestSubmitWithRetry(unittest.TestCase):
    def test_retries_while_tdm_has_no_login_attempt(self):
        calls = []

        def submit(*args):
            calls.append(args)
            if len(calls) < 3:
                raise helper.NoLoginInProgress("gap between attempts")

        with patch.object(helper, "submit_session", side_effect=submit), patch.object(helper.time, "sleep"):
            helper.submit_with_retry("http://tdm", "pw", "tok", "dev")
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_the_retry_window(self):
        with (
            patch.object(helper, "submit_session", side_effect=helper.NoLoginInProgress("x")),
            patch.object(helper, "SUBMIT_RETRY_SEC", 0),
            patch.object(helper.time, "sleep"),
        ):
            with self.assertRaises(helper.NoLoginInProgress):
                helper.submit_with_retry("http://tdm", "pw", "tok", "dev")


class TestPasswordPrompt(unittest.TestCase):
    def test_no_prompt_when_no_password_needed(self):
        prompt = unittest.mock.Mock()
        with patch.object(helper, "submit_with_retry") as submit:
            helper.send_with_password_prompt("http://tdm", None, "tok", "dev", prompt=prompt)
        submit.assert_called_once_with("http://tdm", None, "tok", "dev")
        prompt.assert_not_called()

    def test_prompts_on_401_and_retries_with_the_password(self):
        prompt = unittest.mock.Mock(return_value="pw")
        with patch.object(helper, "submit_with_retry", side_effect=[helper.PasswordRequired(), None]) as submit:
            helper.send_with_password_prompt("http://tdm", None, "tok", "dev", prompt=prompt)
        self.assertEqual(submit.call_args_list[-1].args, ("http://tdm", "pw", "tok", "dev"))

    def test_wrong_password_reprompts_then_gives_up(self):
        prompt = unittest.mock.Mock(return_value="bad")
        with patch.object(helper, "submit_with_retry", side_effect=helper.PasswordRequired()):
            with self.assertRaises(RuntimeError):
                helper.send_with_password_prompt("http://tdm", None, "tok", "dev", prompt=prompt)
        self.assertEqual(prompt.call_count, 3)


class TestRunsViaExec(unittest.TestCase):
    def test_module_has_no_file_dependency(self):
        source = (pathlib.Path(__file__).parent.parent / "scripts" / "tdm_login_helper.py").read_text(encoding="utf-8")
        self.assertNotIn("__file__", source)
        self.assertNotIn("import aiohttp", source)


class TestCdpSocketDecoding(unittest.TestCase):
    """Server -> client frames (unmasked), fed through a real socket pair."""

    def setUp(self):
        import socket

        self.ours, self.chrome = socket.socketpair()
        self.cdp = helper.CdpSocket.__new__(helper.CdpSocket)
        self.cdp._sock = self.ours
        self.cdp._buffer = b""
        self.cdp._next_id = 0

    def tearDown(self):
        self.ours.close()
        self.chrome.close()

    @staticmethod
    def _frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 1 << 16:
            head += bytes([126]) + n.to_bytes(2, "big")
        else:
            head += bytes([127]) + n.to_bytes(8, "big")
        return head + payload

    def test_reads_a_16_bit_length_message(self):
        body = json.dumps({"id": 1, "result": {"pad": "x" * 300}}).encode()
        self.chrome.sendall(self._frame(body))
        self.assertEqual(self.cdp._receive()["result"]["pad"], "x" * 300)

    def test_joins_a_fragmented_message(self):
        body = json.dumps({"id": 1, "result": {}}).encode()
        self.chrome.sendall(self._frame(body[:5], fin=False) + self._frame(body[5:], opcode=0x0))
        self.assertEqual(self.cdp._receive(), {"id": 1, "result": {}})

    def test_close_frame_means_the_browser_was_closed(self):
        self.chrome.sendall(self._frame(b"", opcode=0x8))
        with self.assertRaises(helper.BrowserClosed):
            self.cdp._receive()

    def test_eof_means_the_browser_was_closed(self):
        self.chrome.close()
        with self.assertRaises(helper.BrowserClosed):
            self.cdp._receive()

    def test_call_skips_events_and_returns_its_own_reply(self):
        self.chrome.sendall(
            self._frame(json.dumps({"method": "Network.event", "params": {}}).encode())
            + self._frame(json.dumps({"id": 1, "result": {"cookies": []}}).encode())
        )
        self.assertEqual(self.cdp.call("Storage.getCookies"), {"id": 1, "result": {"cookies": []}})


class TestWindowsConnectionResetMeansBrowserClosed(unittest.TestCase):
    def test_reset_while_reading_is_reported_as_closed_window(self):
        cdp = helper.CdpSocket.__new__(helper.CdpSocket)
        cdp._sock = unittest.mock.Mock()
        cdp._sock.recv.side_effect = ConnectionResetError(10054, "reset")
        cdp._buffer = b""
        cdp._next_id = 0
        with self.assertRaises(helper.BrowserClosed):
            cdp._receive()

    def test_reset_while_sending_is_reported_as_closed_window(self):
        cdp = helper.CdpSocket.__new__(helper.CdpSocket)
        cdp._sock = unittest.mock.Mock()
        cdp._sock.sendall.side_effect = ConnectionAbortedError(10053, "aborted")
        cdp._buffer = b""
        cdp._next_id = 0
        with self.assertRaises(helper.BrowserClosed):
            cdp.call("Storage.getCookies")


class TestPasswordCheckedBeforeTheBrowserOpens(unittest.TestCase):
    def test_accepted_password_is_returned(self):
        with patch.object(helper, "check_password", side_effect=[helper.PasswordRequired(), None]):
            password = helper.ensure_password("http://tdm", None, prompt=unittest.mock.Mock(return_value="pw"))
        self.assertEqual(password, "pw")

    def test_no_password_needed(self):
        prompt = unittest.mock.Mock()
        with patch.object(helper, "check_password", return_value=None):
            self.assertIsNone(helper.ensure_password("http://tdm", None, prompt=prompt))
        prompt.assert_not_called()

    def test_gives_up_after_three_wrong_passwords(self):
        with patch.object(helper, "check_password", side_effect=helper.PasswordRequired()):
            with self.assertRaises(RuntimeError):
                helper.ensure_password("http://tdm", None, prompt=unittest.mock.Mock(return_value="bad"))

    def test_check_password_treats_400_as_accepted(self):
        import io
        import urllib.error

        error = urllib.error.HTTPError("http://tdm", 400, "bad request", {}, io.BytesIO(b""))
        opener = unittest.mock.Mock()
        opener.open.side_effect = error
        with patch.object(helper.urllib.request, "build_opener", return_value=opener):
            helper.check_password("http://tdm", "pw")  # no exception


if __name__ == "__main__":
    unittest.main()
