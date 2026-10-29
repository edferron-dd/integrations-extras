# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Integration-style tests that exercise the full check against realistic
Genesys Cloud Analytics Conversation Detail Query API responses.

There is no self-hostable Genesys dependency, so instead of a Docker Compose
environment these tests replay documented API payloads (see tests/fixtures)
through the real ``PureCloudPlatformClientV2`` SDK deserializer. That gives the
check genuine SDK model objects to walk, so the SDK field contract
(``mediaStatsMinConversationMos`` -> ``media_stats_min_conversation_mos``,
etc.), the pagination loop, cross-page dedupe, threshold math, and metric /
service-check submission are all validated end to end. The tests also assert
that the query the check *sends* matches the documented API contract.

Response shape reference:
https://developer.genesys.cloud/analyticsdatamanagement/analytics/detail/conversation-query
"""

import pytest
from mock import MagicMock, patch

from datadog_checks.genesys_mos import GenesysMosCheck

from .common import read_fixture

pytestmark = pytest.mark.integration

RESPONSE_TYPE = "AnalyticsConversationQueryResponse"
EXPECTED_TAGS = ["region:mypurecloud.com", "team:voice"]


class _FakeRestResponse(object):
    """Minimal stand-in for the SDK's RESTResponse. ``deserialize`` reads the
    raw JSON body from ``.data`` and inspects the ``Content-Type`` header to
    decide whether to JSON-decode it."""

    def __init__(self, data):
        self.data = data

    def getheader(self, name, default=None):
        if name.lower() == "content-type":
            return "application/json"
        return default

    def getheaders(self):
        return {"Content-Type": "application/json"}


def _load_response(api_client, fixture_name):
    """Deserialize a fixture into a real SDK response object."""
    raw = read_fixture(fixture_name)
    return api_client.deserialize(_FakeRestResponse(raw), RESPONSE_TYPE)


def _analytics_api_returning(pages):
    """Build a fake AnalyticsApi that pages through ``pages`` by page_number and
    records every query body it received."""
    api = MagicMock()
    calls = []

    def _query(body):
        calls.append(body)
        index = body.paging.page_number - 1
        return pages[index] if index < len(pages) else pages[-1]

    api.post_analytics_conversations_details_query.side_effect = _query
    api.received_queries = calls
    return api


def _apis(analytics_api, routing_api=None, users_api=None):
    """Bundle the fake APIs the way ``_connect`` does for a real run."""
    apis = MagicMock()
    apis.analytics = analytics_api
    if routing_api is not None:
        apis.routing = routing_api
    if users_api is not None:
        apis.users = users_api
    return apis


def _run_check(instance, analytics_api, routing_api=None, users_api=None):
    check = GenesysMosCheck("genesys_mos", {}, [instance])
    apis = _apis(analytics_api, routing_api, users_api)
    with patch.object(check, "_connect", return_value=apis):
        check.check(None)
    return check


def test_documented_pagination_dedupe_and_metrics(aggregator, instance):
    import PureCloudPlatformClientV2 as genesys

    api_client = genesys.api_client.ApiClient()
    pages = [
        _load_response(api_client, "conversations_page1.json"),
        _load_response(api_client, "conversations_page2.json"),
        _load_response(api_client, "conversations_empty.json"),
    ]
    analytics_api = _analytics_api_returning(pages)

    _run_check(instance, analytics_api)

    # Page 1: 4.42, 4.05 | Page 2: 4.05 (duplicate id), 3.61 -> 3 unique conversations.
    values = [4.42, 4.05, 3.61]
    aggregator.assert_metric("genesys_mos.conversation.count", value=3, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.mos.avg", value=sum(values) / len(values), tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.mos.min", value=3.61, tags=EXPECTED_TAGS)
    # threshold 4.2 -> 4.05 and 3.61 are at/below.
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", value=2, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.can_connect", value=1, tags=EXPECTED_TAGS)
    aggregator.assert_all_metrics_covered()

    # Paged until an empty page was returned (3 requests: page 1, 2, and the empty page 3).
    assert len(analytics_api.received_queries) == 3


def test_query_body_matches_genesys_api_contract(aggregator, instance):
    import PureCloudPlatformClientV2 as genesys

    api_client = genesys.api_client.ApiClient()
    analytics_api = _analytics_api_returning([_load_response(api_client, "conversations_empty.json")])

    _run_check(instance, analytics_api)

    body = analytics_api.received_queries[0]

    # Trailing RFC3339 window "<start>/<end>".
    start, end = body.interval.split("/")
    assert start < end

    # AND filter over the two documented "exists" predicates.
    conversation_filter = body.conversation_filters[0]
    assert conversation_filter.type == "and"
    predicates = {p.dimension: p.operator for p in conversation_filter.predicates}
    assert predicates == {
        "mediaStatsMinConversationMos": "exists",
        "conversationEnd": "exists",
    }

    # Paging starts at page 1 and respects the API's 100-row cap.
    assert body.paging.page_size == 100
    assert body.paging.page_number == 1


def test_conversation_without_mos_is_skipped(aggregator, instance):
    import PureCloudPlatformClientV2 as genesys

    api_client = genesys.api_client.ApiClient()
    pages = [
        _load_response(api_client, "conversations_partial_mos.json"),
        _load_response(api_client, "conversations_empty.json"),
    ]
    analytics_api = _analytics_api_returning(pages)

    _run_check(instance, analytics_api)

    # One conversation carries MOS (4.30); the other has no mediaStatsMinConversationMos
    # and must be skipped rather than counted as a zero.
    aggregator.assert_metric("genesys_mos.conversation.count", value=1, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.mos.avg", value=4.30, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.mos.min", value=4.30, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", value=0, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.can_connect", value=1, tags=EXPECTED_TAGS)


def _entity(entity_id, name):
    entity = MagicMock()
    entity.id = entity_id
    entity.name = name
    return entity


def _paged_listing(entities):
    """A listing endpoint that returns ``entities`` then an empty page, matching
    how the routing/users APIs terminate pagination."""
    listing = MagicMock()
    listing.entities = entities
    empty = MagicMock()
    empty.entities = []
    return MagicMock(side_effect=[listing, empty])


def test_conversation_detail_is_logged_with_resolved_names(aggregator, instance):
    """Replay a realistic conversation-detail payload through the SDK
    deserializer and assert the emitted logs carry the columns the Genesys
    interactions view shows: date, duration, direction, queue, wrap-up, users,
    remote and MOS."""
    import PureCloudPlatformClientV2 as genesys

    instance["collect_logs"] = True
    api_client = genesys.api_client.ApiClient()
    pages = [
        _load_response(api_client, "conversations_detail.json"),
        _load_response(api_client, "conversations_empty.json"),
    ]
    analytics_api = _analytics_api_returning(pages)

    routing_api = MagicMock()
    routing_api.get_routing_queues = _paged_listing(
        [
            _entity("aaaaaaa1-0000-4000-8000-000000000001", "Support_Outbound"),
            _entity("aaaaaaa2-0000-4000-8000-000000000002", "Support_Inbound_BL"),
        ]
    )
    routing_api.get_routing_wrapupcodes = _paged_listing(
        [
            _entity("bbbbbbb1-0000-4000-8000-000000000001", "No Message Left"),
            _entity("bbbbbbb2-0000-4000-8000-000000000002", "No Answer"),
        ]
    )
    users_api = MagicMock()
    users_api.get_users = _paged_listing(
        [
            _entity("ccccccc1-0000-4000-8000-000000000001", "Avery Stone"),
            _entity("ccccccc2-0000-4000-8000-000000000002", "Blair Nunez"),
        ]
    )

    check = GenesysMosCheck("genesys_mos", {}, [instance])
    with (
        patch.object(check, "_connect", return_value=_apis(analytics_api, routing_api, users_api)),
        patch.object(check, "send_log") as send_log,
    ):
        check.check(None)

    logs = {call.args[0]["conversation_id"]: call.args[0] for call in send_log.call_args_list}
    assert len(logs) == 2

    # The transferred, two-agent conversation: both queues, both wrap-up codes,
    # both users, and sessions in both directions.
    transferred = logs["11111111-1111-4111-8111-111111111111"]
    assert transferred["date"] == "2026-10-22T18:46:00+00:00"
    assert transferred["conversation_duration"] == "4m 28s"
    assert transferred["direction"] == "inbound/outbound"
    assert transferred["queue"] == "Support_Outbound, Support_Inbound_BL"
    assert transferred["wrap_up"] == "No Message Left, No Answer"
    assert transferred["users"] == "Avery Stone, Blair Nunez"
    assert transferred["remote"] == "Springfield IL"
    assert transferred["mos"] == 2.9
    assert transferred["status"] == "warning"
    assert transferred["media_type"] == "voice"

    # The simple outbound call.
    outbound = logs["22222222-2222-4222-8222-222222222222"]
    assert outbound["conversation_duration"] == "51s"
    assert outbound["direction"] == "outbound"
    assert outbound["queue"] == "Support_Outbound"
    assert outbound["wrap_up"] == "No Message Left"
    assert outbound["users"] == "Avery Stone"
    # An unnamed external party renders the way the interactions view does.
    assert outbound["remote"] == "Customer"
    assert outbound["status"] == "info"

    # Metrics are unchanged by log collection.
    aggregator.assert_metric("genesys_mos.conversation.count", value=2, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.mos.min", value=2.9, tags=EXPECTED_TAGS)
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", value=1, tags=EXPECTED_TAGS)


def test_logs_are_off_by_default(aggregator, instance):
    import PureCloudPlatformClientV2 as genesys

    api_client = genesys.api_client.ApiClient()
    pages = [
        _load_response(api_client, "conversations_detail.json"),
        _load_response(api_client, "conversations_empty.json"),
    ]
    check = GenesysMosCheck("genesys_mos", {}, [instance])
    with (
        patch.object(check, "_connect", return_value=_apis(_analytics_api_returning(pages))),
        patch.object(check, "send_log") as send_log,
    ):
        check.check(None)

    assert send_log.call_count == 0
    aggregator.assert_metric("genesys_mos.conversation.count", value=2, tags=EXPECTED_TAGS)
