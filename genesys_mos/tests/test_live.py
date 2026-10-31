# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Live tests against a real Genesys Cloud org.

Skipped unless GENESYS_CLIENT_ID and GENESYS_CLIENT_SECRET are set, so they
never run in CI. GENESYS_REGION defaults to mypurecloud.com.
"""

import os

import pytest

from datadog_checks.genesys_mos import GenesysMosCheck

CLIENT_ID = os.environ.get("GENESYS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GENESYS_CLIENT_SECRET")
REGION = os.environ.get("GENESYS_REGION", "mypurecloud.com")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (CLIENT_ID and CLIENT_SECRET),
        reason="GENESYS_CLIENT_ID and GENESYS_CLIENT_SECRET are not set",
    ),
]


@pytest.fixture
def live_instance():
    return {
        "region": REGION,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        # A wide window makes it likely the query returns real conversations.
        "min_collection_interval": int(os.environ.get("GENESYS_WINDOW_SECONDS", 86400)),
        "collect_logs": True,
    }


def test_live_check(aggregator, dd_run_check, live_instance):
    check = GenesysMosCheck("genesys_mos", {}, [live_instance])
    dd_run_check(check)

    tags = ["region:{}".format(REGION)]
    aggregator.assert_metric("genesys_mos.can_connect", value=1, tags=tags)
    aggregator.assert_metric("genesys_mos.conversation.count", tags=tags)
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", tags=tags)

    count = aggregator.metrics("genesys_mos.conversation.count")[0].value
    print("conversations in window: {}".format(count))
    if count:
        aggregator.assert_metric("genesys_mos.conversation.mos.avg", tags=tags)
        aggregator.assert_metric("genesys_mos.conversation.mos.min", tags=tags)
