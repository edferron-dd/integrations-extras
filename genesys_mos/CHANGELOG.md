# CHANGELOG - Genesys Cloud MOS

<!-- towncrier release notes start -->

## 1.1.0

***Added***:

* Add optional per-conversation log collection (`collect_logs`), emitting the conversation duration, direction, queue, wrap-up, users, remote, date and MOS for each ended conversation.
* Resolve queue, wrap-up code and agent ids to display names via the Genesys routing and users APIs, controlled by `resolve_names`.

## 1.0.0

***Added***:

* Initial release of the Genesys Cloud MOS integration.
