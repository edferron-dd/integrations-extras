# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from datetime import datetime, timedelta, timezone

from datadog_checks.base import AgentCheck, ConfigurationError

# MOS (Mean Opinion Score) is reported directly by Genesys Cloud on the
# `mediaStatsMinConversationMos` dimension of the Analytics Conversation Detail
# Query API. It is the minimum MOS observed on a conversation and ranges roughly
# 1.0 (unusable) to ~4.9 (toll quality); it is NOT computed by this check.
MOS_DIMENSION = "mediaStatsMinConversationMos"
CONVERSATION_END_DIMENSION = "conversationEnd"

# Segment dimension used to restrict the call count to calls. MOS only exists
# for voice, so this is also the population the MOS metrics are drawn from.
MEDIA_TYPE_DIMENSION = "mediaType"
VOICE_MEDIA_TYPE = "voice"

# Page size cap enforced by the Genesys analytics API.
PAGE_SIZE = 100

# The call count only reads `totalHits` off the response, so it asks for the
# smallest page the API allows rather than pulling a record per call.
COUNT_PAGE_SIZE = 1

# Page size cap enforced by the routing/users configuration APIs, which are
# queried in bulk to turn the ids on a conversation into display names.
LOOKUP_PAGE_SIZE = 100

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_LAG_SECONDS = 60
DEFAULT_MOS_THRESHOLD = 4.2

# Participant purposes that represent an agent handling the interaction. These
# are the users shown in the "Users" column of the Genesys interactions view.
AGENT_PURPOSES = ("agent", "user")

# Genesys reports the remote party as a separate participant. "Remote" in the
# interactions view is that participant's name, or its purpose when the name is
# not resolvable (e.g. an unidentified external caller shows as "Customer").
REMOTE_PURPOSES = ("customer", "external", "outbound")


class GenesysApis(object):
    """The three Genesys Cloud APIs this check talks to, built from one
    authenticated client: analytics for the conversation query, routing and
    users to resolve queue / wrap-up / agent ids into display names."""

    def __init__(self, analytics, routing, users):
        self.analytics = analytics
        self.routing = routing
        self.users = users


class GenesysMosCheck(AgentCheck):
    __NAMESPACE__ = "genesys_mos"

    CAN_CONNECT_METRIC = "can_connect"

    def __init__(self, name, init_config, instances):
        super(GenesysMosCheck, self).__init__(name, init_config, instances)

        self.region = self.instance.get("region")
        self.client_id = self.instance.get("client_id")
        self.client_secret = self.instance.get("client_secret")
        self.mos_threshold = float(self.instance.get("mos_threshold", DEFAULT_MOS_THRESHOLD))
        self.lag_seconds = int(self.instance.get("collection_lag_seconds", DEFAULT_LAG_SECONDS))
        self.window_seconds = int(self.instance.get("min_collection_interval", DEFAULT_INTERVAL_SECONDS))
        self.collect_logs = bool(self.instance.get("collect_logs", False))
        self.resolve_names = bool(self.instance.get("resolve_names", True))

        if not self.region:
            raise ConfigurationError("`region` is required (e.g. mypurecloud.com, usw2.pure.cloud).")
        if not self.client_id or not self.client_secret:
            raise ConfigurationError("`client_id` and `client_secret` are required.")

        # Low-cardinality tags only. Never tag by conversation_id.
        self.base_tags = ["region:{}".format(self.region)]
        self.base_tags.extend(self.instance.get("tags", []))

        # Genesys configuration objects (queues, wrap-up codes, users) change far
        # more slowly than conversations, so names resolved on one run are reused
        # on later runs. Only ids still missing are looked up.
        self._queue_names = {}
        self._wrapup_names = {}
        self._user_names = {}

    def check(self, _):
        interval = self._query_interval()

        try:
            apis = self._connect()
            conversations = self._collect_conversations(apis.analytics, interval)
            call_count = self._count_calls(apis.analytics, interval)
        except Exception:
            # Report connectivity as a 0/1 gauge rather than a service check.
            self.gauge(self.CAN_CONNECT_METRIC, 0, tags=self.base_tags)
            self.log.exception("Genesys Cloud MOS collection failed")
            raise

        self.gauge(self.CAN_CONNECT_METRIC, 1, tags=self.base_tags)
        self.gauge("call.count", call_count, tags=self.base_tags)
        self._submit_metrics(conversations)

        if self.collect_logs:
            self._submit_conversation_logs(apis, conversations)

    def _query_interval(self):
        """Trailing, interval-length window offset by a lag so ended conversations
        have been indexed by Genesys. Consecutive runs cover ~disjoint windows,
        which avoids double-counting the distribution across runs."""
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=self.lag_seconds)
        start = end - timedelta(seconds=self.window_seconds)
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        return "{}/{}".format(start.strftime(fmt), end.strftime(fmt))

    def _authenticate(self):
        """Authenticate with the client-credentials grant and return an ApiClient.
        A fresh token is fetched each run, so no long-lived refresh logic is needed.
        Isolated for testability (patched in unit tests)."""
        import PureCloudPlatformClientV2 as genesys

        # `Configuration` is a singleton, and the SDK shadows its own
        # `configuration` submodule with an instance of it at import time, so the
        # host must be set on the class from the package root. The ApiClient
        # constructor reads that host, so it has to be set first.
        genesys.Configuration().host = "https://api.{}".format(self.region)
        return genesys.ApiClient().get_client_credentials_token(self.client_id, self.client_secret)

    def _connect(self):
        """Build the API bindings used by a run from a single authenticated client."""
        import PureCloudPlatformClientV2 as genesys

        api_client = self._authenticate()
        return GenesysApis(
            analytics=genesys.AnalyticsApi(api_client),
            routing=genesys.RoutingApi(api_client),
            users=genesys.UsersApi(api_client),
        )

    def _collect_conversations(self, analytics_api, interval):
        """Page through the conversation detail query and return
        {conversation_id: conversation}. Dedupes by conversation_id to guard
        against overlap between pages/windows. Conversations without a MOS value
        are dropped, since MOS is what this check reports on."""
        import PureCloudPlatformClientV2 as genesys

        mos_predicate = genesys.ConversationDetailQueryPredicate()
        mos_predicate.dimension = MOS_DIMENSION
        mos_predicate.operator = "exists"

        conversation_filter = _ended_conversation_filter(genesys, mos_predicate)

        collected = {}
        page_number = 1
        while True:
            body = genesys.ConversationQuery()
            body.interval = interval
            body.conversation_filters = [conversation_filter]
            paging = genesys.PagingSpec()
            paging.page_size = PAGE_SIZE
            paging.page_number = page_number
            body.paging = paging

            response = analytics_api.post_analytics_conversations_details_query(body)
            conversations = getattr(response, "conversations", None)
            if not conversations:
                break

            for conversation in conversations:
                mos = getattr(conversation, "media_stats_min_conversation_mos", None)
                conversation_id = getattr(conversation, "conversation_id", None)
                if mos is None or conversation_id is None:
                    continue
                collected[conversation_id] = conversation

            page_number += 1

        return collected

    def _count_calls(self, analytics_api, interval):
        """Number of ended voice calls in the window, with or without a MOS
        value. Genesys reports the match count as `totalHits` on every page, so
        one single-record request is enough; no paging through the calls.

        Calls that never carried audio (unanswered, failed, abandoned before
        connecting) have no MOS, which is what makes this differ from
        `conversation.count`. The window and the "ended" rule are the same as
        the MOS query's, so the two counts are directly comparable."""
        import PureCloudPlatformClientV2 as genesys

        voice_predicate = genesys.SegmentDetailQueryPredicate()
        voice_predicate.dimension = MEDIA_TYPE_DIMENSION
        voice_predicate.operator = "matches"
        voice_predicate.value = VOICE_MEDIA_TYPE

        segment_filter = genesys.SegmentDetailQueryFilter()
        segment_filter.type = "and"
        segment_filter.predicates = [voice_predicate]

        body = genesys.ConversationQuery()
        body.interval = interval
        body.conversation_filters = [_ended_conversation_filter(genesys)]
        body.segment_filters = [segment_filter]
        paging = genesys.PagingSpec()
        paging.page_size = COUNT_PAGE_SIZE
        paging.page_number = 1
        body.paging = paging

        response = analytics_api.post_analytics_conversations_details_query(body)
        # Genesys omits `totalHits` when nothing matched.
        return getattr(response, "total_hits", None) or 0

    def _submit_metrics(self, conversations):
        count = len(conversations)
        self.gauge("conversation.count", count, tags=self.base_tags)

        values = [float(c.media_stats_min_conversation_mos) for c in conversations.values()]
        below_threshold = sum(1 for mos in values if mos <= self.mos_threshold)
        self.gauge("conversation.below_threshold.count", below_threshold, tags=self.base_tags)

        # Only emit quality gauges when there is data; emitting 0 would look like a
        # catastrophic MOS rather than "no conversations in the window".
        if count:
            self.gauge("conversation.mos.avg", sum(values) / count, tags=self.base_tags)
            self.gauge("conversation.mos.min", min(values), tags=self.base_tags)

        self.log.debug(
            "Genesys MOS: %d conversations, %d at/below threshold %.2f",
            count,
            below_threshold,
            self.mos_threshold,
        )

    # --- Per-conversation logs -------------------------------------------------

    def _submit_conversation_logs(self, apis, conversations):
        """Emit one log per conversation carrying the same columns the Genesys
        interactions view shows: date, duration, direction, queue, wrap-up,
        users, remote and MOS. One log per conversation keeps the detail out of
        metric tags, where conversation-level cardinality would be unusable."""
        if not conversations:
            return

        if self.resolve_names:
            try:
                self._resolve_names(apis, conversations.values())
            except Exception as e:
                # Names are presentation only. A failed lookup must not cost us
                # the logs themselves, which still carry the raw ids.
                self.log.warning("Could not resolve Genesys queue/wrap-up/user names; logging ids instead: %s", e)

        for conversation in conversations.values():
            self.send_log(self._build_log(conversation))

        self.log.debug("Genesys MOS: submitted %d conversation logs", len(conversations))

    def _build_log(self, conversation):
        """Flatten one conversation into the log payload. Every value is a string
        so the fields stay stable in the log explorer facets, except `mos` and
        `duration` which stay numeric to be graphable."""
        start = getattr(conversation, "conversation_start", None)
        end = getattr(conversation, "conversation_end", None)
        mos = float(conversation.media_stats_min_conversation_mos)

        agent_user_ids = []
        remote_names = []
        queue_ids = []
        wrapup_ids = []
        directions = []
        media_types = []

        for participant in getattr(conversation, "participants", None) or []:
            purpose = (getattr(participant, "purpose", None) or "").lower()

            if purpose in AGENT_PURPOSES:
                user_id = getattr(participant, "user_id", None)
                if user_id:
                    _append_unique(agent_user_ids, user_id)

            session_remote = None
            for session in getattr(participant, "sessions", None) or []:
                _append_unique(directions, (getattr(session, "direction", None) or "").lower())
                _append_unique(media_types, getattr(session, "media_type", None))
                session_remote = session_remote or (
                    getattr(session, "remote_name_displayable", None) or getattr(session, "remote", None)
                )

                for segment in getattr(session, "segments", None) or []:
                    _append_unique(queue_ids, getattr(segment, "queue_id", None))
                    _append_unique(wrapup_ids, getattr(segment, "wrap_up_code", None))

            if purpose in REMOTE_PURPOSES:
                # The displayable remote name wins, then the participant name,
                # then the purpose -- which is how the interactions view renders
                # an external party it cannot name (e.g. "Customer").
                _append_unique(
                    remote_names,
                    session_remote or getattr(participant, "participant_name", None) or purpose.capitalize(),
                )

        entry = {
            "ddsource": "genesys-mos",
            "message": "Genesys conversation {} MOS {:.2f}".format(conversation.conversation_id, mos),
            "status": "warning" if mos <= self.mos_threshold else "info",
            "conversation_id": conversation.conversation_id,
            "region": self.region,
            "mos": mos,
            "mos_threshold": self.mos_threshold,
            "below_mos_threshold": mos <= self.mos_threshold,
            "direction": _direction(directions, getattr(conversation, "originating_direction", None)),
            "originating_direction": getattr(conversation, "originating_direction", None) or "",
            "queue": ", ".join(self._names(self._queue_names, queue_ids)),
            "queue_ids": ", ".join(queue_ids),
            "wrap_up": ", ".join(self._names(self._wrapup_names, wrapup_ids)),
            "wrap_up_ids": ", ".join(wrapup_ids),
            "users": ", ".join(self._names(self._user_names, agent_user_ids)),
            "user_ids": ", ".join(agent_user_ids),
            "remote": ", ".join(remote_names),
            "media_type": ", ".join(t for t in media_types if t),
        }

        if start is not None:
            entry["date"] = _isoformat(start)
            # `timestamp` places the log at the time the conversation started
            # rather than at collection time, so the log explorer timeline lines
            # up with the interactions view.
            entry["timestamp"] = _epoch_seconds(start)
        if end is not None:
            entry["conversation_end"] = _isoformat(end)
        if start is not None and end is not None:
            duration = (end - start).total_seconds()
            entry["duration"] = round(duration, 3)
            entry["conversation_duration"] = _format_duration(duration)

        return entry

    def _resolve_names(self, apis, conversations):
        """Look up the display names for every queue, wrap-up code and agent id
        seen this run that is not already cached."""
        queue_ids = set()
        wrapup_ids = set()
        user_ids = set()

        for conversation in conversations:
            for participant in getattr(conversation, "participants", None) or []:
                purpose = (getattr(participant, "purpose", None) or "").lower()
                user_id = getattr(participant, "user_id", None)
                if purpose in AGENT_PURPOSES and user_id:
                    user_ids.add(user_id)
                for session in getattr(participant, "sessions", None) or []:
                    for segment in getattr(session, "segments", None) or []:
                        queue_id = getattr(segment, "queue_id", None)
                        if queue_id:
                            queue_ids.add(queue_id)
                        wrapup_id = getattr(segment, "wrap_up_code", None)
                        if wrapup_id:
                            wrapup_ids.add(wrapup_id)

        lookups = (
            ("queue", "Routing > Queue > View", self._queue_names, queue_ids, apis.routing.get_routing_queues),
            (
                "wrap-up code",
                "Routing > Wrapup Code > View",
                self._wrapup_names,
                wrapup_ids,
                apis.routing.get_routing_wrapupcodes,
            ),
            ("user", "Directory > User > View", self._user_names, user_ids, apis.users.get_users),
        )
        # Each entity type needs its own OAuth permission, so one missing
        # permission must not stop the other names from resolving.
        for entity, permission, cache, ids, list_endpoint in lookups:
            try:
                self._fetch_names(cache, ids, list_endpoint)
            except Exception as e:
                self.log.warning(
                    "Could not resolve Genesys %s names (the OAuth client's role needs %s); logging ids instead: %s",
                    entity,
                    permission,
                    _describe_error(e),
                )

    def _fetch_names(self, cache, ids, list_endpoint):
        """Populate `cache` with {id: name} for the ids it is missing.

        All three Genesys listing endpoints accept an `id` filter. Each batch is
        at most one page of ids, so a single request per batch returns every
        match. Do not page past it: with an `id` filter Genesys returns the same
        entities for every page_number, so paging until an empty page never
        terminates and hangs the check run."""
        missing = sorted(i for i in ids if i not in cache)
        if not missing:
            return

        for offset in range(0, len(missing), LOOKUP_PAGE_SIZE):
            batch = missing[offset : offset + LOOKUP_PAGE_SIZE]
            listing = list_endpoint(id=batch, page_size=LOOKUP_PAGE_SIZE, page_number=1)
            for entity in getattr(listing, "entities", None) or []:
                entity_id = getattr(entity, "id", None)
                name = getattr(entity, "name", None)
                if entity_id and name:
                    cache[entity_id] = name

    @staticmethod
    def _names(cache, ids):
        """Display names for `ids`, falling back to the raw id when a lookup did
        not return one (deleted queue, name resolution disabled, lookup failed)."""
        return [cache.get(i, i) for i in ids]


def _ended_conversation_filter(genesys, *predicates):
    """AND filter matching conversations that have ended, plus `predicates`.
    Shared by the MOS query and the call count so both cover the same calls."""
    end_predicate = genesys.ConversationDetailQueryPredicate()
    end_predicate.dimension = CONVERSATION_END_DIMENSION
    end_predicate.operator = "exists"

    conversation_filter = genesys.ConversationDetailQueryFilter()
    conversation_filter.type = "and"
    conversation_filter.predicates = list(predicates) + [end_predicate]
    return conversation_filter


def _describe_error(error):
    """Short form of a Genesys SDK error. ApiException carries the HTTP status
    and reason, which is what tells a missing permission (403) apart from the
    rest; its full string form also dumps headers and the response body."""
    status = getattr(error, "status", None)
    if status is not None:
        return "HTTP {} {}".format(status, getattr(error, "reason", "") or "").strip()
    return "{}: {}".format(type(error).__name__, error)


def _append_unique(items, value):
    if value and value not in items:
        items.append(value)


def _direction(directions, originating_direction):
    """The interactions view shows "Inbound/Outbound" for a conversation whose
    sessions ran in both directions, and the single direction otherwise."""
    if len(directions) > 1:
        return "/".join(sorted(directions))
    if directions:
        return directions[0]
    return (originating_direction or "").lower()


def _format_duration(seconds):
    """Render a duration the way the interactions view does: "51s", "4m 28s",
    "1h 9m 58s"."""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append("{}h".format(hours))
    if hours or minutes:
        parts.append("{}m".format(minutes))
    parts.append("{}s".format(secs))
    return " ".join(parts)


def _isoformat(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _epoch_seconds(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return None
