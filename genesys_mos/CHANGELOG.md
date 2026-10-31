# CHANGELOG - Genesys Cloud MOS

<!-- towncrier release notes start -->

## 1.1.3

***Fixed***:

* Fix the check hanging on every run once `collect_logs` was enabled. The name lookups paged until an empty page, but Genesys returns the same entities for every page when an `id` filter is set, so the loop never ended and no metrics or logs were submitted. Each id batch is now fetched with a single request.

## 1.1.2

***Fixed***:

* Add the required `logs` section (`type: integration`) to the configuration example and docs. Without it the Agent drops every log the check emits and warns `file is nil for integration ID`.
* Resolve queue, wrap-up code and user names independently, so one missing OAuth permission no longer blocks the other lookups, and include the entity type, required permission and HTTP status in the warning.

## 1.1.1

***Fixed***:

* Fix an `AttributeError` on every check run when setting the API host. The SDK shadows its own `configuration` submodule with a `Configuration` instance at import time, so the host is now set via the `Configuration` singleton exported at the package root.

## 1.1.0

***Added***:

* Add optional per-conversation log collection (`collect_logs`), emitting the conversation duration, direction, queue, wrap-up, users, remote, date and MOS for each ended conversation.
* Resolve queue, wrap-up code and agent ids to display names via the Genesys routing and users APIs, controlled by `resolve_names`.

## 1.0.0

***Added***:

* Initial release of the Genesys Cloud MOS integration.
