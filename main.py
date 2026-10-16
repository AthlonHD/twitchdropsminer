#!/usr/bin/env python3
"""
TwitchDropsMiner - Main entry point

This is a simple launcher that runs the src package as a module.
All application code is in the src/ directory.
"""

if __name__ == "__main__":
    import faulthandler, runpy, signal

    faulthandler.register(signal.SIGUSR1, all_threads=True)

    # Run the src package as a module
    runpy.run_module("src", run_name="__main__")
