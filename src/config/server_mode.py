"""Small, backwards-compatible server hooks used by the HD Tool integration."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerMode:
    host: str = "0.0.0.0"
    remote_login: bool = False

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ServerMode:
        env = os.environ if environ is None else environ
        host = env.get("TDM_HOST", "0.0.0.0").strip()
        if not host:
            raise ValueError("TDM_HOST must not be empty")
        return cls(host, env.get("TDM_REMOTE_LOGIN", "").strip().lower() in {"1", "true", "yes"})
