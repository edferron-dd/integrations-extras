# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import re
from datetime import datetime, timezone

import pytest
from mock import MagicMock, patch

from datadog_checks.base import ConfigurationError
from datadog_checks.dev.utils import get_metadata_metrics
from datadog_checks.genesys_mos import GenesysMosCheck

pytestmark = pytest.mark.unit


def _check(instance):
    return GenesysMosCheck("genesys_mos", {}, [instance])


def _conversation(conversation_id, mos, **attrs):
    """A stand-in for one SDK conversation model, carrying only the attributes
    the check reads."""
    conversation = MagicMock()
    conversation.conversation_id = conversation_id
    conversation.media_stats_min_conversation_mos = mos
    conversation.conversation_start = attrs.get("start")
    conversation.conversation_end = attrs.get("end")
    conversation.originating_direction = attrs.get("originating_direction")
    conversation.participants = attrs.get("participants", [])
    return conversation


def _conversations(mos_by_id):
    return {cid: _conversation(cid, mos) for cid, mos in mos_by_id.items()}


def test_configuration_models_load_from_spec(instance):
    # Exercise the spec-generated config models the way the Agent does before a
    # run (via check_initializations), validating that a well-formed instance
    # maps cleanly onto InstanceConfig/SharedConfig.
    check = _check(instance)
    check.load_configuration_models()

    assert check._config_model_shared is not None
    assert check._config_model_instance is not None
    assert check._config_model_instance.region == "mypurecloud.com"
    assert check._config_model_instance.client_id == "test-client-id"
    assert check._config_model_instance.mos_threshold == 4.2
    assert check._config_model_instance.min_collection_interval == 300
    assert check._config_model_instance.tags == ("team:voice",)


def test_config_requires_region(instance):
    del instance["region"]
    with pytest.raises(ConfigurationError, match="region"):
        _check(instance)


def test_config_requires_credentials(instance):
    del instance["client_secret"]
    with pytest.raises(ConfigurationError, match="client_id"):
        _check(instance)


def test_query_interval_is_rfc3339_range(instance):
    check = _check(instance)
    interval = check._query_interval()
    start, end = interval.split("/")
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"
    assert re.match(pattern, start)
    assert re.match(pattern, end)
    assert start < end


def test_emits_distribution_gauges_and_can_connect(aggregator, instance):
    check = _check(instance)
    conversations = _conversations({"c1": 4.8, "c2": 4.1, "c3": 3.5})

    with (
        patch.object(check, "_connect", return_value=MagicMock()),
        patch.object(check, "_collect_conversations", return_value=conversations),
    ):
        check.check(None)

    tags = ["region:mypurecloud.com", "team:voice"]
    aggregator.assert_metric("genesys_mos.conversation.count", value=3, tags=tags)
    aggregator.assert_metric("genesys_mos.conversation.mos.avg", value=(4.8 + 4.1 + 3.5) / 3, tags=tags)
    aggregator.assert_metric("genesys_mos.conversation.mos.min", value=3.5, tags=tags)
    # threshold 4.2 -> 4.1 and 3.5 are at/below
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", value=2, tags=tags)
    # Connectivity is reported as a 0/1 gauge, not a service check.
    aggregator.assert_metric("genesys_mos.can_connect", value=1, tags=tags)
    aggregator.assert_all_metrics_covered()
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())


def test_no_conversations_reports_zero(aggregator, instance):
    check = _check(instance)
    with (
        patch.object(check, "_connect", return_value=MagicMock()),
        patch.object(check, "_collect_conversations", return_value={}),
    ):
        check.check(None)

    tags = ["region:mypurecloud.com", "team:voice"]
    aggregator.assert_metric("genesys_mos.conversation.count", value=0, tags=tags)
    aggregator.assert_metric("genesys_mos.conversation.below_threshold.count", value=0, tags=tags)
    # No quality gauges when there is no data.
    aggregator.assert_metric("genesys_mos.conversation.mos.avg", count=0)
    aggregator.assert_metric("genesys_mos.conversation.mos.min", count=0)
    aggregator.assert_metric("genesys_mos.can_connect", value=1)


def test_auth_failure_reports_can_connect_zero_and_raises(aggregator, instance):
    check = _check(instance)
    with patch.object(check, "_connect", side_effect=RuntimeError("bad secret")):
        with pytest.raises(RuntimeError, match="bad secret"):
            check.check(None)

    aggregator.assert_metric("genesys_mos.can_connect", value=0)
    aggregator.assert_metric("genesys_mos.conversation.count", count=0)


def test_collect_conversations_pages_and_dedupes(instance):
    check = _check(instance)

    def make_conversation(cid, mos):
        conv = MagicMock()
        conv.conversation_id = cid
        conv.media_stats_min_conversation_mos = mos
        return conv

    page1 = MagicMock()
    page1.conversations = [make_conversation("c1", 4.5), make_conversation("c2", 3.9)]
    page2 = MagicMock()
    # c2 repeated across page boundary -> must dedupe
    page2.conversations = [make_conversation("c2", 3.9), make_conversation("c3", 4.9)]
    page3 = MagicMock()
    page3.conversations = None

    analytics_api = MagicMock()
    analytics_api.post_analytics_conversations_details_query.side_effect = [page1, page2, page3]

    # Avoid importing the real Genesys SDK: inject a stub module whose classes
    # are permissive MagicMock factories.
    genesys_stub = MagicMock()
    with patch.dict("sys.modules", {"PureCloudPlatformClientV2": genesys_stub}):
        result = check._collect_conversations(analytics_api, "start/end")

    assert {cid: c.media_stats_min_conversation_mos for cid, c in result.items()} == {
        "c1": 4.5,
        "c2": 3.9,
        "c3": 4.9,
    }
    assert analytics_api.post_analytics_conversations_details_query.call_count == 3


# --- Conversation logs ---------------------------------------------------------


def _session(direction, media_type="voice", remote=None, segments=()):
    session = MagicMock()
    session.direction = direction
    session.media_type = media_type
    session.remote_name_displayable = remote
    session.remote = None
    session.segments = list(segments)
    return session


def _segment(queue_id=None, wrap_up_code=None, segment_type="interact"):
    segment = MagicMock()
    segment.queue_id = queue_id
    segment.wrap_up_code = wrap_up_code
    segment.segment_type = segment_type
    return segment


def _participant(purpose, user_id=None, name=None, sessions=()):
    participant = MagicMock()
    participant.purpose = purpose
    participant.user_id = user_id
    participant.participant_name = name
    participant.sessions = list(sessions)
    return participant


def _entity(entity_id, name):
    entity = MagicMock()
    entity.id = entity_id
    entity.name = name
    return entity


def _listing(entities):
    listing = MagicMock()
    listing.entities = entities
    return listing


def test_log_carries_every_interactions_view_column(instance):
    instance["collect_logs"] = True
    check = _check(instance)
    check._queue_names["q1"] = "Support_Outbound"
    check._wrapup_names["w1"] = "No Message Left"
    check._user_names["u1"] = "Avery Stone"

    conversation = _conversation(
        "conv-1",
        2.9,
        start=datetime(2026, 10, 22, 18, 46, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 22, 18, 50, 28, tzinfo=timezone.utc),
        originating_direction="inbound",
        participants=[
            _participant(
                "agent",
                user_id="u1",
                sessions=[_session("inbound", segments=[_segment(queue_id="q1", wrap_up_code="w1")])],
            ),
            _participant("customer", sessions=[_session("inbound", remote="Springfield IL")]),
        ],
    )

    entry = check._build_log(conversation)

    assert entry["conversation_id"] == "conv-1"
    assert entry["date"] == "2026-10-22T18:46:00+00:00"
    # 4m 28s, matching how the interactions view renders the duration.
    assert entry["conversation_duration"] == "4m 28s"
    assert entry["duration"] == 268.0
    assert entry["direction"] == "inbound"
    assert entry["queue"] == "Support_Outbound"
    assert entry["wrap_up"] == "No Message Left"
    assert entry["users"] == "Avery Stone"
    assert entry["remote"] == "Springfield IL"
    assert entry["mos"] == 2.9
    # 2.9 is at/below the 4.2 threshold.
    assert entry["below_mos_threshold"] is True
    assert entry["status"] == "warning"
    assert entry["region"] == "mypurecloud.com"
    assert entry["ddsource"] == "genesys-mos"
    # The log is stamped with the conversation start, not collection time.
    assert entry["timestamp"] == conversation.conversation_start.timestamp()


def test_log_joins_multiple_users_queues_and_directions(instance):
    check = _check(instance)
    check._queue_names.update({"q1": "Collections_OUT", "q2": "Collections_INB"})
    check._wrapup_names.update({"w1": "No Message Left", "w2": "No Answer"})
    check._user_names.update({"u1": "Avery Stone", "u2": "Blair Nunez"})

    conversation = _conversation(
        "conv-2",
        4.4,
        start=datetime(2026, 10, 22, 18, 46, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 22, 18, 46, 51, tzinfo=timezone.utc),
        originating_direction="inbound",
        participants=[
            _participant(
                "agent",
                user_id="u1",
                sessions=[_session("inbound", segments=[_segment(queue_id="q1", wrap_up_code="w1")])],
            ),
            _participant(
                "agent",
                user_id="u2",
                sessions=[_session("outbound", segments=[_segment(queue_id="q2", wrap_up_code="w2")])],
            ),
        ],
    )

    entry = check._build_log(conversation)

    assert entry["users"] == "Avery Stone, Blair Nunez"
    assert entry["queue"] == "Collections_OUT, Collections_INB"
    assert entry["wrap_up"] == "No Message Left, No Answer"
    # Sessions ran both ways, which the interactions view shows as Inbound/Outbound.
    assert entry["direction"] == "inbound/outbound"
    assert entry["conversation_duration"] == "51s"
    assert entry["status"] == "info"
    assert entry["below_mos_threshold"] is False


def test_log_falls_back_to_ids_when_names_are_unknown(instance):
    check = _check(instance)
    conversation = _conversation(
        "conv-3",
        4.4,
        start=datetime(2026, 10, 22, 18, 46, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 22, 19, 55, 58, tzinfo=timezone.utc),
        originating_direction="outbound",
        participants=[
            _participant(
                "agent",
                user_id="u-unknown",
                sessions=[_session("outbound", segments=[_segment(queue_id="q-unknown", wrap_up_code="w-unknown")])],
            ),
        ],
    )

    entry = check._build_log(conversation)

    assert entry["queue"] == "q-unknown"
    assert entry["queue_ids"] == "q-unknown"
    assert entry["wrap_up"] == "w-unknown"
    assert entry["users"] == "u-unknown"
    assert entry["conversation_duration"] == "1h 9m 58s"


def test_logs_are_not_submitted_unless_collect_logs_is_enabled(aggregator, instance):
    check = _check(instance)
    with (
        patch.object(check, "_connect", return_value=MagicMock()),
        patch.object(check, "_collect_conversations", return_value=_conversations({"c1": 4.8})),
        patch.object(check, "send_log") as send_log,
    ):
        check.check(None)

    assert send_log.call_count == 0


def test_one_log_per_conversation_when_enabled(aggregator, instance):
    instance["collect_logs"] = True
    instance["resolve_names"] = False
    check = _check(instance)
    conversations = _conversations({"c1": 4.8, "c2": 3.1})

    with (
        patch.object(check, "_connect", return_value=MagicMock()),
        patch.object(check, "_collect_conversations", return_value=conversations),
        patch.object(check, "send_log") as send_log,
    ):
        check.check(None)

    assert send_log.call_count == 2
    logged = {call.args[0]["conversation_id"]: call.args[0] for call in send_log.call_args_list}
    assert set(logged) == {"c1", "c2"}
    assert logged["c2"]["status"] == "warning"
    assert logged["c1"]["status"] == "info"


def test_names_are_looked_up_in_one_batch_per_entity_type(instance):
    instance["collect_logs"] = True
    check = _check(instance)

    conversation = _conversation(
        "conv-4",
        4.4,
        participants=[
            _participant(
                "agent",
                user_id="u1",
                sessions=[_session("inbound", segments=[_segment(queue_id="q1", wrap_up_code="w1")])],
            ),
            _participant(
                "agent",
                user_id="u2",
                sessions=[_session("inbound", segments=[_segment(queue_id="q1", wrap_up_code="w1")])],
            ),
        ],
    )

    apis = MagicMock()
    apis.routing.get_routing_queues.side_effect = [_listing([_entity("q1", "Collections_OUT")]), _listing(None)]
    apis.routing.get_routing_wrapupcodes.side_effect = [_listing([_entity("w1", "No Answer")]), _listing(None)]
    apis.users.get_users.side_effect = [
        _listing([_entity("u1", "Avery Stone"), _entity("u2", "Blair Nunez")]),
        _listing(None),
    ]

    check._resolve_names(apis, [conversation])

    assert check._queue_names == {"q1": "Collections_OUT"}
    assert check._wrapup_names == {"w1": "No Answer"}
    assert check._user_names == {"u1": "Avery Stone", "u2": "Blair Nunez"}
    # Ids repeated across participants are deduped into a single batched lookup.
    assert apis.users.get_users.call_args_list[0].kwargs["id"] == ["u1", "u2"]
    assert apis.routing.get_routing_queues.call_args_list[0].kwargs["id"] == ["q1"]

    # A second run reuses the cache rather than re-querying.
    apis.routing.get_routing_queues.side_effect = None
    apis.routing.get_routing_queues.reset_mock()
    check._resolve_names(apis, [conversation])
    assert apis.routing.get_routing_queues.call_count == 0


def test_name_lookup_failure_still_logs_with_ids(aggregator, instance):
    instance["collect_logs"] = True
    check = _check(instance)
    conversations = _conversations({"c1": 4.8})

    with (
        patch.object(check, "_connect", return_value=MagicMock()),
        patch.object(check, "_collect_conversations", return_value=conversations),
        patch.object(check, "_resolve_names", side_effect=RuntimeError("missing routing scope")),
        patch.object(check, "send_log") as send_log,
    ):
        check.check(None)

    assert send_log.call_count == 1
    aggregator.assert_metric("genesys_mos.can_connect", value=1)
