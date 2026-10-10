# Deployment

One Linux server hosts the HTTPS operator page, Airflow and PostgreSQL, and
executes generation, independent MuJoCo verification and PR publication. One
logged-in Windows computer with licensed SolidWorks serializes native input
freezing, discovery and capture. Operators use **Feishu login → engineering folder → Start → checks,
URDF preview and review PR**.

Use the same source-bound tool release on both hosts. This guide owns installation
and maintenance; [operations](operations.md) owns the engineering workflow.

## Prerequisites

| Host or service | Required preparation |
|---|---|
| Linux | Ubuntu 22.04 x86_64, user-level systemd, Git, GitHub CLI, network access to the Windows worker, GitHub and Feishu |
| Windows | SolidWorks 2026 (native major 34; other versions require platform acceptance first), Python 3.12 x86_64 and OpenSSH Server; an interactive desktop session |
| Model repository | Private repository, an existing `feature/<hardware>` base and a dedicated clean Linux clone |
| Feishu | Enterprise app, approved tenant keys, registered OAuth callback and access to basic user identity/profile |
| Storage | Dedicated CAD intake directories; separate frozen inputs, outputs, state, secrets and model clones |

Choose one operator address through `OPERATOR_HOST` and the HTTPS port, for
example `https://operator.example.com:8443/`. Register that address followed by
`/auth/feishu/callback` as the app callback.
The browser must trust the server certificate and be able to reach this address.
Use the same origin for the app homepage, login and callback. Feishu's redirect
URL list permits callbacks; its order does not select the address. The platform
derives the callback from `OPERATOR_HOST` and the HTTPS port. Avoid environment
overrides that point it elsewhere. The proxy redirects login and callback
requests with a different hostname to the configured origin before authentication.

Operators use their existing enterprise Feishu accounts; the platform has no
account-registration step. Create a dedicated enterprise custom app, enable
the **Web app** feature and set its desktop homepage to the operator address.
Register the exact callback under **Security Settings → Redirect URLs**.
Publish a version with the intended users in its availability scope; new
configuration takes effect after publication. A creator-only release is
sufficient for commissioning that user's sign-in.

SolidWorks is required for fresh native discovery and capture. Verification and
rebuild of a complete frozen delivery run on Linux without opening CAD. Both hosts require Python 3.12 x86_64 and the pinned runtime wheels.
Consumer checks load models without rendering; they require no display, GPU or
graphics-driver setup. `description doctor` checks the host role: SolidWorks registration and COM
prerequisites on Windows, pinned dependencies and an isolated consumer load on
Linux. Windows capture requires neither MuJoCo nor GitHub access. A native
readiness pass is a prerequisite; the capture session still checks actual CAD
access and document readiness.
Windows jobs must run as the logged-in execution user, outside Session 0.

## 1. Install the Windows worker

Download the Windows offline runtime archive and verify its SHA-256 against the
release manifest. Extract it, then run from the extracted directory:

```powershell
py -3.12 -m venv C:\description-runtime
C:\description-runtime\Scripts\python.exe -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock
C:\description-runtime\Scripts\description.exe doctor
```

Create the directories below. `C:\cad-handoffs` contains engineering sources;
the worker collects immutable copies into `C:\description-packages`. Source
roots must be narrow, real directories and separate from managed storage.

```powershell
New-Item -ItemType Directory -Force C:\cad-handoffs, C:\cad-records, C:\description-packages, C:\description-deliveries, C:\description-state, C:\description-secrets | Out-Null
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls C:\description-secrets /inheritance:r /grant:r "${account}:(OI)(CI)F"
C:\description-runtime\Scripts\python.exe -c "import secrets; from pathlib import Path; Path('C:/description-secrets/endpoint.token').write_text(secrets.token_hex(32), encoding='ascii')"
```

Keep the token private. Transfer it securely to the Linux host for the tunnel's
Airflow Connection and portal; do not put its value in source code or logs.
Save `C:\description-state\endpoint.json`:

```json
{
  "schema_version": "solidworks-to-urdf.endpoint/v1",
  "handoff_roots": ["C:/cad-handoffs"],
  "package_root": "C:/description-packages",
  "output_root": "C:/description-deliveries",
  "state_root": "C:/description-state/jobs",
  "token_file": "C:/description-secrets/endpoint.token",
  "host": "127.0.0.1",
  "port": 8765,
  "discovery": {
    "record_roots": ["C:/cad-records"]
  },
  "targets": {
    "arm": {
      "repository_slug": "<owner>/<model-repository>",
      "base": "feature/arm"
    }
  }
}
```

Replace `arm` with the CAD's `dp.hardware_id` and configure its model base.
The endpoint rejects unconfigured hardware. Configure `discovery.record_roots`
for the controlled specification library, for example `["C:/cad-records"]`.
Populate the library with approved records before admitting jobs.
CAD references include the approved record version, such as
`arm/r2/budget.json#robot`, resolved as `C:/cad-records/arm/r2/budget.json`.
Keep versions immutable and available together; do not replace a shared file
between runs. Use real directories and files, without symbolic links or junctions.
Each reference must match one file across the configured roots;
duplicate matches block discovery even when their bytes agree. Files included
with an operator's handoff are archived, but do not supply this authority.
Optional `discovery.frozen_names_file` validates approved interface names.
Neither setting is an operator input.

Start the endpoint in the logged-in desktop:

```powershell
C:\description-runtime\Scripts\description.exe serve --config C:\description-state\endpoint.json
```

Keep Windows awake during processing. After foreground acceptance, an
interactive Scheduled Task may start this same command at login under the same
user. Do not create a second worker or run concurrent native CAD jobs.

## 2. Configure Linux and Feishu


Configure Git identity and GitHub authentication for the Linux service user.
Create a dedicated clean model checkout; the publication stage verifies that its
origin matches the Windows hardware routing's repository slug:

```sh
gh auth login
gh auth setup-git
git clone https://github.com/<owner>/<model-repository> /srv/description/models/arm
```

Extract the deployment archive matching the worker release. Keep deployment
configuration and secrets outside the checkout. Copy
[`operator.env.example`](../deploy/operator/operator.env.example) to a private
file, then set its actual paths and host:

```sh
description_base="$HOME/description"
description_env="$description_base/operator.env"
mkdir -p "$description_base/secrets"
chmod 700 "$description_base" "$description_base/secrets"
cp deploy/operator/operator.env.example "$description_env"
chmod 600 "$description_env"
```

Replace the example `/srv/description` paths with your chosen service directory.
Complete these configuration groups once:

| Settings | Purpose |
|---|---|
| `OPERATOR_HOST`, `OPERATOR_HTTPS_PORT`, `OPERATOR_TLS_CERT`, `OPERATOR_TLS_KEY` | One operator address and its trusted HTTPS identity |
| `OPERATOR_STATE`, `AIRFLOW_VENV`, `AIRFLOW_HOME`, `POSTGRES_ROOT` | Separate runtime, database and managed state |
| `PIPELINE_WHEEL`, `AIRFLOW_DB_URL` | Matching release wheel and dedicated PostgreSQL connection |
| `SOLIDWORKS_SSH_HOST`, `SOLIDWORKS_ENDPOINT_PORT` | Key-authenticated SSH alias to Windows and its loopback endpoint |
| `ENDPOINT_TOKEN_FILE` | Private copy of the Windows endpoint token, mode `0600` |
| `SOLIDWORKS_HANDOFF_ROOT` | Dedicated Linux intake, such as `/srv/description/cad-handoffs`, outside runtime and state |
| `FEISHU_APP_SECRET_FILE`, `FEISHU_TENANT_KEYS` | App credentials and mandatory tenant allowlist |
| `FEISHU_ADMIN_OPEN_IDS` | Explicit administrator identities; optional, no automatic administrator |

Use comma-separated tenant and administrator lists without spaces.
The configuration example defines the remaining service and transport defaults.

Set `OPERATOR_TLS_CERT` and `OPERATOR_TLS_KEY` to the absolute paths of the
approved certificate chain and its private key. The certificate must cover
`OPERATOR_HOST`; the service user must be able to read both files, and the key
must have mode `0600`. Reinstall and restart the services after replacing either
file. Leaving both settings empty generates a self-signed certificate for a LAN
rehearsal; shared use requires browser trust in the approved certificate.

Store the app credentials in the mode-`0600` JSON file named by
`FEISHU_APP_SECRET_FILE`:

```json
{"app_id": "<enterprise-app-id>", "app_secret": "<enterprise-app-secret>"}
```

Obtain the company identifier with the app's **Tenant token** permission
`tenant:tenant:readonly` (**Obtain tenant information**). Exchange the app
credentials for a tenant access token, then call
[Obtain company information](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/tenant-v2/tenant/query).
Set `FEISHU_TENANT_KEYS` from `data.tenant.tenant_key` after confirming the
company name. The company name and `display_id` are not tenant keys.

Feishu API authentication is required for the workflow. The server reads the
authenticated user's Feishu username and keeps credentials server-side.
The enterprise app is used for sign-in only. The
[current authorization API](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)
uses an S256 challenge; the server exchanges the code at the
[OAuth token API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)
with a JSON request containing the PKCE verifier.
The [basic profile API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get)
requires no additional contact-directory, email, phone or employment permissions.
The workflow does not request offline access or retain Feishu refresh tokens.

After an approved administrator signs in, obtain that person's app-scoped
`open_id` from `/auth/feishu/profile` and add it to `FEISHU_ADMIN_OPEN_IDS`.
Keep the list empty until an administrator is explicitly selected. User IDs
from another app cannot be reused. Apply configuration through the supported
installation and restart steps below.

Membership in an approved tenant grants workflow operator access; administrators
must also appear in the explicit admin list. Operator access covers starting new
runs and viewing shared results; the run's initiator (stable authenticated
identity) and platform administrators can retry that run through the single
contextual Retry action for positively classified transport recovery, which
continues the same DAG run and native job without recapturing or editing frozen
inputs or artifacts. Native terminal failures require a new run after correcting
inputs or configuration. The owner check ships with the platform package; no
separate credential or install-time flag is required, and operator run changes
are refused when that guard is absent. Each run's history and details show the
original submitter's Feishu username from the authenticated API response.
The app, tenant and `open_id` remain the internal audit identity; users do not
supply the submitter name. Feishu does not supply CAD, drive specifications or
engineering approval merely through login.

Configure the SSH alias on Linux using a dedicated execution key and verified
Windows host key. Confirm it works without a password prompt:

```sh
ssh -o BatchMode=yes solidworks-worker whoami
```

The endpoint binds only to Windows loopback. The managed SSH tunnel exposes it
on Linux loopback port `18765`; operators never configure transport or tokens.

## 3. Install and start Linux services

From the deployment archive:

```sh
bash deploy/operator/operatorctl.sh install --env-file "$description_env"
bash deploy/operator/operatorctl.sh start --env-file "$description_env"
bash deploy/operator/operatorctl.sh health --env-file "$description_env"
```

Installation provisions the pinned Python toolchain, PostgreSQL 14, Airflow
3.3.2, the matching tool wheel, proxy and service configuration. It starts the
managed database before migration; `start` launches the remaining services.
Installation creates the sole `solidworks_windows` Connection with the endpoint
token and Linux source allowlist. Operators need no Connection or DAG setup.

`install` writes configuration, secrets and service units while preserving the
DAG's admission state. `start` opens and verifies DAG admission before starting
the operator page and HTTPS proxy; `stop` closes ingress and pauses the DAG before
stopping the core services. Start and health check installed configuration for
drift. Reinstall after a configuration change;
existing signing and encryption keys are preserved. For services to survive a
Linux user logout, the host administrator runs the following command with the
actual service account:

```sh
sudo loginctl enable-linger <service-user>
```

Before Feishu configuration, health exits with failure for missing SSO
credentials and unavailable SSO commissioning (`HTTP 503`). These identify an
incomplete installation. Any additional failure requires investigation. A
self-signed rehearsal certificate also requires browser trust before use;
service liveness does not establish that trust. Commissioning requires zero
health failures, trusted HTTPS and a successful live Feishu sign-in.

## Acceptance

Commission the complete workflow with actual native CAD and live Feishu:

1. Run `description doctor` in each installed runtime and require all checks to
   pass. Confirm HTTPS trust, all services, authenticated Windows endpoint health
   and the installed Airflow Connection.
2. Sign in with Feishu. Verify the displayed Feishu username, approved tenant,
   explicit admin assignment and denied unauthorized users. Confirm an approved
   operator can start new pipeline runs and view shared results from other
   operators.
3. As the run's initiator, confirm the Retry action appears only for a
   positively classified transport failure and continues the same DAG run and
   native job without recapturing or editing frozen inputs or artifacts, and is
   absent for native terminal failures. Confirm a platform administrator can
   also retry that run, that another approved user can view the run but cannot
   retry it, and that the Retry request
   (`POST /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/clear` with
   `dry_run`, `only_failed: true`, `only_new: false`,
   `run_on_latest_version: false`) is accepted for the initiator and refused
   for another approved user.
4. In the operator page, choose the complete engineering folder once (Chrome or
   Edge); the browser uploads its files to the platform's Linux intake and
   `Start` creates the run. No server path, YAML, branch, hardware or
   credential field is entered by hand; selections containing SolidWorks lock
   transients (`~$…`) are rejected before anything runs.
5. Verify the UUID, frozen inventory and all six engineering steps. Inspect
   input/input QC/output/output QC in the page, the DAG contract table,
   terminal task logs and `engineering_stages` XCom. Confirm failed, blocked,
   not-run states and external engineering review scope, and the hashed `reports/stages.json`.
   Verify the actual native discovery and every independent quality result.
   Confirm history and details show the original submitter's Feishu username
   after reload and when viewed by another authorized operator.
6. Inspect the delivered URDF and actual meshes in the page. Exercise individual
   joint controls and limits; confirm preview binds to the passing file subject.
7. Check the resulting private-model PR's exact base, head, structural revision
   and verified commit. Candidate submission does not grant engineering approval.
8. Exercise a transport-recovery Retry together with automatic polling, plus
   changed inputs, quality failure, service restart and PR failure. Retain
   diagnostics; a PR-service failure preserves verified preview.
9. After the complete service restart described below, select a completed run
   and choose **从此步骤重新运行** from verification or publication. Require a
   new linked run with the selected starting step recorded in the endpoint
   request. Upstream results must show **复用已验证结果**, retain their original
   timestamps and producing-run identity, and trigger no new CAD capture.
   Confirm that only the selected step and its downstream steps execute again,
   and that the original run and evidence remain unchanged. Verify access for
   the initiator and administrators, refusal for other operators, and an
   actionable refusal when upstream evidence or tool identity is invalid.

Mocks, server liveness and an unconfigured OAuth callback do not establish this
acceptance. Retain reports for the exact tool and native source revision.

## Maintenance

The upload root also holds `.run-metadata.json`, the portal's durable display
names and deleted-record state. Back it up with platform state and preserve it
across upgrades. Moving a run to **已删除** does not reclaim CAD or artifact storage.
Unreadable history metadata blocks run history and its management; repair or restore the
metadata instead of discarding it and losing the recorded names and deletions.

In a new shell, set `description_env` to the installed configuration file:

```sh
description_env="$HOME/description/operator.env"
bash deploy/operator/operatorctl.sh status --env-file "$description_env"
bash deploy/operator/operatorctl.sh health --env-file "$description_env"
bash deploy/operator/operatorctl.sh stop --env-file "$description_env"
```

Service logs use `journalctl --user -u <service>`. Maintainers read Airflow task
logs under the configured `AIRFLOW_HOME/logs` on Linux; no separate Airflow UI
is exposed to operators. Task logs, endpoint state and bound job diagnostics
identify failed operations. Transport retries reconnect
to the existing native job. A terminal native failure requires a new run; an
endpoint restart during capture marks that job failed. Preserve its partial
evidence for diagnosis. Recover a publication failure from the complete verified
delivery using [the operations procedure](operations.md#6-independent-review-and-recovery).
Engineering corrections require a new source revision and run.

After code or deployment changes, run the
[development checks](../CONTRIBUTING.md#verification), validate shell syntax and
repeat affected native and deployed acceptance. For an upgrade, let the current
native job finish and stop new submissions. Retain the previous release archives,
runtime directories and private configuration, including `operator.env`, its
`PIPELINE_WHEEL` path and the Windows endpoint configuration. Stop the endpoint
and Linux services, install the same new release on both hosts, then restart the
endpoint and Linux services and run health. `install` updates files; it does not
restart an already running process.

If acceptance fails, drain native work and stop both hosts' services. Restore the
previous tool installations and private configuration, run `install` from the
previous deployment archive, then start the endpoint and Linux services. Verify
both installed source identities and run health before reopening submissions.
Preserve every frozen input, delivery, diagnostic, signing key, job and ledger
row created during the upgrade. Do not restore an older database or state snapshot;
code rollback requires compatible persisted schemas. Published tags and assets
remain immutable.
