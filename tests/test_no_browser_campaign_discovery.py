import importlib.util

from src.services import inventory_service


def test_inventory_fetch_does_not_launch_a_discovery_browser():
    # It spun up a Playwright Chromium on every inventory fetch to widen the
    # old device-code (SMARTBOX) campaign list and found nothing for the web
    # identity; with working integrity tokens the Campaigns query is complete.
    assert not hasattr(inventory_service, "discover_campaigns_via_browser")
    assert importlib.util.find_spec("src.services.campaign_discovery") is None
