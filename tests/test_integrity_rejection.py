import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.auth.auth_state import INTEGRITY_FAILURE_COOLDOWN, _AuthState


class TestRejectIntegrityToken(unittest.TestCase):
    """A token Twitch answers with IntegrityCheckFailed must not be reused
    until its (up to 4h) expiry -- drop it so the next validate() mints a
    fresh one, but don't mint one per request when fresh ones keep failing."""

    def _auth_with_token(self, minted_ago: timedelta) -> _AuthState:
        auth = _AuthState(MagicMock())
        minted = datetime.now(timezone.utc) - minted_ago
        with patch("src.auth.auth_state.datetime") as dt:
            dt.now.return_value = minted
            auth._adopt_integrity_token(("itok", minted + timedelta(hours=4)))
        return auth

    def test_rejected_old_token_is_dropped_for_immediate_renewal(self):
        auth = self._auth_with_token(timedelta(hours=1))
        auth.reject_integrity_token()
        self.assertIsNone(auth._integrity_token)
        self.assertIsNone(auth._integrity_expires_at)
        failed_until = auth._integrity_failed_until
        self.assertTrue(failed_until is None or failed_until <= datetime.now(timezone.utc))

    def test_rejected_fresh_token_enters_the_failure_cooldown(self):
        auth = self._auth_with_token(timedelta(seconds=30))
        auth.reject_integrity_token()
        self.assertIsNone(auth._integrity_token)
        self.assertGreater(auth._integrity_failed_until, datetime.now(timezone.utc))
        self.assertLessEqual(
            auth._integrity_failed_until, datetime.now(timezone.utc) + INTEGRITY_FAILURE_COOLDOWN
        )

    def test_noop_without_a_token(self):
        auth = _AuthState(MagicMock())
        auth.reject_integrity_token()
        self.assertIsNone(auth._integrity_failed_until)
