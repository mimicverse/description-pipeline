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
| Windows | SolidWorks 2026, Python 3.12 x86_64, Git, GitHub CLI and OpenSSH Server; an interactive desktop session |
| Model repository | Private repository, an existing `feature/<hardware>` base and a dedicated clean Windows clone |
| Feishu | Enterprise app, approved tenant keys, registered OAuth callback and access to basic user identity/profile |
| Storage | Dedicated CAD intake directories; separate frozen inputs, outputs, state, secrets and model clones |

The commissioning server uses `https://10.0.0.235:8443/`. Register
`https://10.0.0.235:8443/auth/feishu/callback` as the app callback. For another
host, register the callback derived from its `OPERATOR_HOST` and HTTPS port.
The browser must trust the server certificate and be able to reach this address.

SolidWorks is required for fresh native discovery and capture. Verification and
rebuild of a complete frozen delivery run on Linux or Windows without opening
CAD. Windows jobs must run as the logged-in execution user, outside Session 0.

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
mkdir -p /home/andy/operator/secrets
chmod 700 /home/andy/operator /home/andy/operator/secrets
cp deploy/operator/operator.env.example /home/andy/operator/operator.env
chmod 600 /home/andy/operator/operator.env
```

Complete these configuration groups once:

| Settings | Purpose |
|---|---|
| `OPERATOR_HOST`, `OPERATOR_HTTPS_PORT`, TLS certificate/key | One operator address and its trusted HTTPS identity |
| `OPERATOR_STATE`, `AIRFLOW_VENV`, `AIRFLOW_HOME`, `POSTGRES_ROOT` | Separate runtime, database and managed state |
| `PIPELINE_WHEEL`, `AIRFLOW_DB_URL` | Matching release wheel and dedicated PostgreSQL connection |
| `SOLIDWORKS_SSH_HOST`, `SOLIDWORKS_ENDPOINT_PORT` | Key-authenticated SSH alias to Windows and its loopback endpoint |
| `ENDPOINT_TOKEN_FILE` | Private copy of the Windows endpoint token, mode `0600` |
| `SOLIDWORKS_HANDOFF_ROOT` | Dedicated Linux intake, such as `/home/andy/cad-handoffs`, outside runtime and state |
| `FEISHU_APP_SECRET_FILE`, `FEISHU_TENANT_KEYS` | App credentials and mandatory tenant allowlist |
| `FEISHU_ADMIN_OPEN_IDS` | Explicit administrator identities; optional, no automatic administrator |

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
ssh -o BatchMode=yes windows-m3 whoami
```

The endpoint binds only to Windows loopback. The managed SSH tunnel exposes it
on Linux loopback port `18765`; operators never configure transport or tokens.

## 3. Install and start Linux services

From the deployment archive:

```sh
bash deploy/operator/operatorctl.sh install --env-file /home/andy/operator/operator.env
bash deploy/operator/operatorctl.sh start --env-file /home/andy/operator/operator.env
bash deploy/operator/operatorctl.sh health --env-file /home/andy/operator/operator.env
```

Installation provisions the pinned Python toolchain, PostgreSQL 14, Airflow
3.3.2, the matching tool wheel, proxy and service configuration. It creates the
sole `solidworks_windows` Connection with the endpoint token and Linux source
allowlist. Operators need no Connection or DAG setup.

Only `install` writes configuration, secrets or service units. Start and health
check installed configuration for drift. Reinstall after a configuration change;
existing signing and encryption keys are preserved. For services to survive a
Linux user logout, the host administrator enables lingering for the service user.

A generated self-signed certificate supports a LAN rehearsal. Use an approved,
browser-trusted certificate for the shared operator address. Credentials missing
from an installed app must produce an explicit unavailable login, never a
password fallback or a readiness pass.

## Acceptance

Commission the complete workflow with actual native CAD and live Feishu:

1. Confirm HTTPS trust, all services, authenticated Windows endpoint health and
   the installed Airflow Connection.
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

```sh
bash deploy/operator/operatorctl.sh status --env-file /home/andy/operator/operator.env
bash deploy/operator/operatorctl.sh health --env-file /home/andy/operator/operator.env
bash deploy/operator/operatorctl.sh stop --env-file /home/andy/operator/operator.env
```

Service logs use `journalctl --user -u <service>`. Airflow logs, endpoint state
and bound job diagnostics identify failed operations. Resolve infrastructure
failures and retry the same frozen execution when safe; change the source
revision and create a new run after engineering corrections.

After code or deployment changes, run the
[development checks](../CONTRIBUTING.md#verification), validate shell syntax and
repeat affected native and deployed acceptance. Install both hosts from the same
new release; published tags and assets remain immutable.
