"""Login form manager for handling Twitch authentication."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.i18n import _


if TYPE_CHECKING:
    from src.web.gui_manager import WebGUIManager
    from src.web.managers.broadcaster import WebSocketBroadcaster


class LoginFormManager:
    """Login status for the dashboard: logged in/out, the device code, or a
    login attempt that is waiting either in a local browser window or for the
    login helper."""

    def __init__(self, broadcaster: WebSocketBroadcaster, manager: WebGUIManager):
        self._broadcaster = broadcaster
        self._manager = manager
        self._status = _.t["login"]["status"]["logged_out"]
        self._user_id: int | None = None
        self._user_login: str | None = None
        self._login_helper = False
        self._device_code: dict[str, str] | None = None

    def update(self, status: str, user_id: int | None, user_login: str | None = None):
        self._status = status
        self._user_id = user_id
        self._user_login = user_login
        self._login_helper = False
        self._device_code = None
        asyncio.create_task(
            self._broadcaster.emit(
                "login_status", {"status": status, "user_id": user_id, "user_login": user_login}
            )
        )

    async def ask_enter_code(self, url: str, code: str) -> None:
        """Show the device-code login: the user enters `code` at `url` on any device.

        One login_status event carries the code itself -- a separate event raced
        with the plain status update and the dashboard could end up hiding it."""
        self._status = _.t["login"]["status"]["waiting_auth"]
        self._user_id = None
        self._user_login = None
        self._login_helper = False
        self._device_code = {"url": url, "code": code}
        await self._broadcaster.emit(
            "login_status",
            {"status": self._status, "user_id": None, "user_login": None, "device_code": self._device_code},
        )

    async def start_browser_login_local(self) -> None:
        """TDM opened a browser window on this computer's own desktop."""
        # Only English.json is guaranteed to carry login.browser_login.
        text = _.t["login"].get("browser_login", {}).get(
            "on_real_display", "A browser window has opened on this computer -- log in to Twitch there."
        )
        self.update(text, None)

    async def start_browser_login_remote(self) -> None:
        """Headless host: the dashboard shows the login-helper command."""
        self._status = _.t["login"]["status"]["required"]
        self._user_id = None
        self._user_login = None
        self._device_code = None
        self._login_helper = True
        await self._broadcaster.emit("login_status", self.get_status())

    def get_status(self) -> dict[str, Any]:
        """Get current login status for client synchronization."""
        result: dict[str, Any] = {"status": self._status, "user_id": self._user_id, "user_login": self._user_login}
        if self._login_helper:
            result["login_helper"] = True
        if self._device_code is not None:
            result["device_code"] = self._device_code
        return result
