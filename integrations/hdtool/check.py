"""Read-only deployment contract check; never patches upstream source files."""
from __future__ import annotations

import ast
import json
from pathlib import Path


class IntegrationCheck:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def run(self):
        manifest = json.loads((self.root / "integrations/hdtool/integration.json").read_text("utf-8"))
        if manifest.get("name") != "hdtool-tdm" or manifest.get("contract_version") != 1:
            raise RuntimeError("Unsupported HD Tool/TDM integration contract")
        for relative in (manifest["entrypoint"], "src/config/server_mode.py", "src/__main__.py", "src/auth/browser_login.py"):
            ast.parse((self.root / relative).read_text("utf-8-sig"), filename=relative)
        # Check the integration's hooks remain wired after an upstream merge.
        for relative in ("src/__main__.py", "src/auth/browser_login.py"):
            code = (self.root / relative).read_text("utf-8-sig")
            if "ServerMode.from_environment()" not in code:
                raise RuntimeError(f"Missing server-mode hook after upstream merge: {relative}")
        print("HD Tool/TDM contract v1: OK (read-only)")


if __name__ == "__main__":
    IntegrationCheck(Path(__file__).resolve().parents[2]).run()
