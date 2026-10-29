see AGENTS.md


## HD Tool fork maintenance

This checkout is the AthlonHD fork, branch `hdtool`; `upstream` is SimpliAj. Follow `docs/hdtool-maintenance.md` and the contract in `integrations/hdtool/README.md`. Keep fork-only multi-user code and tests under `integrations/hdtool/`; HD Tool owns only the Agent process host and Windows job cleanup. Never copy the gateway back into HD Tool or patch source files during startup. Preserve the two `ServerMode` hooks when merging upstream and run the integration tests. Never commit `tdm-platform`, cookies, password files, `.venv`, or local deployment scratch. Production continues to be started by HdTool.Agent.

Optional no-install login is implemented in integrations/hdtool/remote_login.py with authenticated, same-origin display/input transport. It is experimental: screenshot/input tests do not prove Twitch authentication succeeds. Never request Twitch credentials in chat, log them, or mark the feature verified without a real user completing authentication. Keep the local helper fallback.

Remote login must retry Chrome initial-paint capture failures; diagnostic logs may contain only the stage and exception type, never keyboard input or authentication data.

On Windows Desktop Experience, remote web login defaults to a normal visible browser opened only during an explicit user login. Do not silently reintroduce headless mode or claim this resolves Twitch authentication rejection without user verification.

Mining selection is opt-in by default: an empty watch list must never render as all games selected or populate other games on the first inventory click. Run node tests/test_inventory_selection.cjs and the priority-list fallback tests after upstream UI merges. Existing selections may only be cleared when the user requests it.
