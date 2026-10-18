import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any


logger = logging.getLogger("TwitchDrops")


class ManualModeStore:
    """Keeps the manually picked game/channel on disk so manual mode survives a restart."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> dict[str, str] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable manual mode state in %s", self._path)
            return None
        if not isinstance(data, dict) or not data.get("game_name"):
            return None
        return {"game_name": data["game_name"], "channel_login": data.get("channel_login", "")}

    def save(self, game_name: str, channel_login: str) -> None:
        self._path.write_text(
            json.dumps({"game_name": game_name, "channel_login": channel_login}),
            encoding="utf-8",
        )

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)

    @staticmethod
    def find_channel(saved: dict[str, str], channels: Iterable[Any]) -> Any | None:
        """The saved channel if it is still tracked, else any channel of the saved game."""
        game_name = saved["game_name"].lower()
        login = saved.get("channel_login", "").lower()
        same_game = [
            ch for ch in channels if ch.game is not None and ch.game.name.lower() == game_name
        ]
        for channel in same_game:
            if channel.name.lower() == login:
                return channel
        return same_game[0] if same_game else None
