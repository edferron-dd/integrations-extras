# Deploying the Genesys Cloud MOS integration on Windows Server

Install instructions for `datadog_genesys_mos-1.1.0-py3-none-any.whl` on a Windows
Server host running the Datadog Agent.

All commands are PowerShell and must run in a session with **administrator**
privileges.

## Prerequisites

- Datadog Agent v7.21+ (or v6.21+) installed and reporting.
- A Genesys Cloud [OAuth client][1] using the **Client Credentials** grant, with a role
  granting **Analytics > Conversation Detail > View**. For resolved names in the logs the
  role also needs **Routing > Queue > View**, **Routing > Wrapup Code > View**, and
  **Directory > User > View**.
- The built wheel, copied to the server (this guide assumes `C:\Temp\`).

### Paths

| What | Path |
| --- | --- |
| Agent CLI | `%ProgramFiles%\Datadog\Datadog Agent\bin\agent.exe` |
| Embedded Python | `%ProgramFiles%\Datadog\Datadog Agent\embedded3\python.exe` |
| Agent config | `C:\ProgramData\Datadog\datadog.yaml` |
| Check config | `C:\ProgramData\Datadog\conf.d\genesys_mos.d\conf.yaml` |

The `bin\agent.exe` and `embedded3\` paths apply to Agent v6.12 and later. On v6.11 and
earlier, the CLI is at `embedded\agent.exe` and Python at `embedded\python.exe`.

## 1. Check the embedded Python version

The wheel declares `requires-python = ">=3.12"`, so pip refuses to install it on an Agent
whose embedded interpreter is older.

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\embedded3\python.exe" --version
```

If this reports a version below 3.12, either upgrade the Agent to a release that ships
Python 3.12, or lower the `requires-python` floor in `pyproject.toml` and rebuild the
wheel.

## 2. Install the wheel

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\bin\agent.exe" integration install -w C:\Temp\datadog_genesys_mos-1.1.0-py3-none-any.whl
```

If the Agent rejects the wheel because the integration is not one Datadog ships, add
`--third-party` before `-w`:

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\bin\agent.exe" integration install --third-party -w C:\Temp\datadog_genesys_mos-1.1.0-py3-none-any.whl
```

## 3. Install the Genesys Cloud SDK

The check's third-party dependency is not bundled in the wheel, so install it into the
Agent's embedded environment:

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\embedded3\python.exe" -m pip install "PureCloudPlatformClientV2==261.0.0"
```

## 4. Enable log collection

Log collection is disabled in the Agent by default. Edit
`C:\ProgramData\Datadog\datadog.yaml`:

```yaml
logs_enabled: true
```

## 5. Configure the check

Create `C:\ProgramData\Datadog\conf.d\genesys_mos.d\conf.yaml`:

```yaml
instances:
  - region: mypurecloud.com
    client_id: "<CLIENT_ID>"
    client_secret: "<CLIENT_SECRET>"
    min_collection_interval: 300
    collect_logs: true
```

Set `region` to the API host for your organization, without the `api.` prefix (for example
`mypurecloud.com`, `usw2.pure.cloud`, or `mypurecloud.ie`).

Set `resolve_names: false` if you would rather log the raw queue, wrap-up and user ids and
skip the extra API lookups. See [conf.yaml.example][2] for every available option.

### Credentials

Avoid writing the client secret in plaintext. The Agent resolves `ENC[]` secret handles in
check configuration before the check runs, and this works on Windows exactly as it does on
Linux:

```yaml
instances:
  - region: mypurecloud.com
    client_id: ENC[genesys_client_id]
    client_secret: ENC[genesys_client_secret]
    min_collection_interval: 300
    collect_logs: true
```

See the **Secrets management** section of [README.md](README.md) for the backend setup, and
confirm resolution afterwards with:

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\bin\agent.exe" secret
```

That lists each handle and its resolution status without printing the secret values.

## 6. Restart the Agent

```powershell
Restart-Service datadogagent
```

## 7. Verify

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\bin\agent.exe" status
```

Look for `genesys_mos` under the **Checks** section. `Total Runs` should increment with no
errors, and `Metric Samples` should be non-zero.

Then open the Datadog **Logs Explorer** and search `source:genesys-mos`. The first logs
arrive after one collection interval plus the 60-second indexing lag. Each log is
timestamped at the conversation's start time rather than at collection time, so widen the
time range if nothing appears at first.

## Troubleshooting

- **`genesys_mos.can_connect` is `0`** — verify `region`, `client_id` and `client_secret`,
  and confirm the OAuth client's role has **Analytics > Conversation Detail > View**.
- **No logs in Datadog** — confirm `logs_enabled: true` in `datadog.yaml` and
  `collect_logs: true` on the instance, then restart the Agent.
- **Logs show raw ids instead of names** — the OAuth client's role is missing
  **Routing > Queue > View**, **Routing > Wrapup Code > View**, or
  **Directory > User > View**. The check logs a warning and falls back to ids rather than
  dropping the logs.
- **No conversations reported** — MOS is only present on conversations that used media with
  measurable quality (voice), and only after they have ended and been indexed. Increase
  `collection_lag_seconds` if conversations appear to be missed near the window boundary.
- **Check the Agent's own log** at
  `C:\ProgramData\Datadog\logs\agent.log` for Python tracebacks from the check.

## Upgrading

Install the new wheel over the old one with the same `integration install -w` command, then
restart the Agent. Configuration in `conf.d\genesys_mos.d\conf.yaml` is not touched by the
install.

## Uninstalling

```powershell
& "$env:ProgramFiles\Datadog\Datadog Agent\embedded3\python.exe" -m pip uninstall datadog-genesys-mos
Remove-Item -Recurse "C:\ProgramData\Datadog\conf.d\genesys_mos.d"
Restart-Service datadogagent
```

[1]: https://help.mypurecloud.com/articles/create-an-oauth-client/
[2]: datadog_checks/genesys_mos/data/conf.yaml.example
