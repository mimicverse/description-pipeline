# Deployment

One Linux server hosts the HTTPS operator page, Airflow and PostgreSQL. One
logged-in Windows computer with licensed SolidWorks executes native jobs
serially. Operators use **Feishu login → engineering folder → Start → checks,
URDF preview and review PR**.

Use the same source-bound tool release on both hosts. This guide owns installation
and maintenance; [operations](operations.md) owns the engineering workflow.

## Prerequisites

| Host or service | Required preparation |
|---|---|
| Linux | Ubuntu 22.04 x86_64, user-level systemd, network access to the Windows worker, GitHub and Feishu |
| Windows | Qualified SolidWorks version (tested with 2026), Python 3.12 x86_64, Git, GitHub CLI and OpenSSH Server; an interactive desktop session |
| Model repository | Private repository, an existing `feature/<hardware>` base and a dedicated clean Windows clone |
| Feishu | Enterprise app, approved tenant keys, registered OAuth callback and access to basic user identity/profile |
| Storage | Dedicated CAD intake directories; separate frozen inputs, outputs, state, secrets and model clones |

Choose one operator address through `OPERATOR_HOST` and the HTTPS port, for
example `https://operator.example.com:8443/`. Register that address followed by
`/auth/feishu/callback` as the app callback.
The browser must trust the server certificate and be able to reach this address.

Operators use their existing enterprise Feishu accounts; the platform has no
account-registration step. The enterprise app administrator enables the app
for its intended users, registers the callback above and provides its App ID,
private secret file and approved tenant keys to the platform maintainer.

SolidWorks is required for fresh native discovery and capture. Verification and
rebuild of a complete frozen delivery run on Linux or Windows without opening
CAD. Both hosts require Python 3.12 x86_64 and the pinned runtime wheels.
Consumer checks load models without rendering; they require no display, GPU or
graphics-driver setup. `description doctor` exercises the actual consumer loader
in an isolated process, and native jobs check readiness before opening CAD.
Windows jobs must run as the logged-in execution user, outside Session 0.

## 1. Install the Windows worker

Download the Windows offline runtime archive and verify its SHA-256 against the
release manifest. Extract it, then run from the extracted directory:

```powershell
py -3.12 -m venv C:\description-runtime
C:\description-runtime\Scripts\python.exe -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock
C:\description-runtime\Scripts\description.exe doctor
```

Configure Git identity and GitHub authentication for that execution user. Create
a dedicated clone of the private model repository:

```powershell
gh auth login
gh auth setup-git
git clone https://github.com/<owner>/<model-repository> C:\description-models\arm
```

Create the directories below. `C:\cad-handoffs` contains engineering sources;
the worker collects immutable copies into `C:\description-packages`. Source
roots must be narrow, real directories and separate from managed storage.

```powershell
New-Item -ItemType Directory -Force C:\cad-handoffs, C:\description-packages, C:\description-deliveries, C:\description-state, C:\description-secrets | Out-Null
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
  "targets": {
    "arm": {
      "repository": "C:/description-models/arm",
      "base": "feature/arm"
    }
  }
}
```

Replace `arm` with the CAD's `dp.hardware_id` and configure its model base.
The endpoint rejects unconfigured hardware. Optional `discovery.record_roots`
identify controlled specification directories; `discovery.frozen_names_file`
validates approved interface names. Neither setting is an operator input.

Start the endpoint in the logged-in desktop:

```powershell
C:\description-runtime\Scripts\description.exe serve --config C:\description-state\endpoint.json
```

Keep Windows awake during processing. After foreground acceptance, an
interactive Scheduled Task may start this same command at login under the same
user. Do not create a second worker or run concurrent native CAD jobs.

## 2. Configure Linux and Feishu

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

The enterprise app is used for sign-in only. The
[current authorization API](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)
uses an S256 challenge; the server exchanges the code at the
[v3 token API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3).
The [basic profile API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get)
requires no additional contact-directory, email, phone or employment permissions.
The workflow does not request offline access or retain Feishu refresh tokens.

Membership in an
approved tenant grants workflow operator access; administrators must also appear
in the explicit admin list. Identity is bound to the app, tenant and `open_id`,
and recorded with the Airflow execution. Feishu does not supply CAD, drive
specifications or engineering approval merely through login.

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

Only `install` writes configuration, secrets or service units. Start and health
check installed configuration for drift. Reinstall after a configuration change;
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
2. Sign in with Feishu. Verify the displayed identity, approved tenant,
   workflow permissions, explicit admin assignment and denied unauthorized users.
3. Supply a compliant native folder from an approved Linux or Windows source
   root. Start without YAML, branch, hardware or credential fields.
4. Verify the UUID, frozen inventory, actual native discovery, generated
   definition and every independent quality result.
5. Inspect the delivered URDF and actual meshes in the page. Exercise individual
   joint controls and limits; confirm preview binds to the passing file subject.
6. Check the resulting private-model PR's exact base, head, structural revision
   and verified commit. Candidate submission does not grant engineering approval.
7. Exercise retries, changed inputs, quality failure, service restart and PR
   failure. Retain diagnostics; a PR-service failure preserves verified preview.

Mocks, server liveness and an unconfigured OAuth callback do not establish this
acceptance. Retain reports for the exact tool and native source revision.

## Maintenance

In a new shell, set `description_env` to the installed configuration file:

```sh
description_env="$HOME/description/operator.env"
bash deploy/operator/operatorctl.sh status --env-file "$description_env"
bash deploy/operator/operatorctl.sh health --env-file "$description_env"
bash deploy/operator/operatorctl.sh stop --env-file "$description_env"
```

Service logs use `journalctl --user -u <service>`. Airflow logs, endpoint state
and bound job diagnostics identify failed operations. Transport retries reconnect
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
