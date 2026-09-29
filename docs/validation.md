# 0.3.23 validation

Published on 2026-09-29 as [v0.3.23](https://github.com/mimicverse/description-pipeline/releases/tag/v0.3.23). The tag points to source commit `7aa39ca89bda18c95c39336071ee625d183c8221`; both bundles report package digest `17c8688560a4ac96eb9fde911e45a8c9e4f05ba5ac38fc76868fea33cb9445d3`. This is the first public source line. The private `mimicverse/description` repository and its CAD/model branches remain private.

| Gate | Result |
| --- | --- |
| Source quality | Linux: 6/6 steps, 1,214 tests (`skipped=113`). Windows: 8/8 steps, 1,214 tests (`skipped=127`), including deployment and submission PowerShell suites. |
| Dependencies and source | No known advisories in the 27 Linux, 29 Windows development, or 17 Windows runtime pins. `gitleaks` found no leaks in the two public commits or the exported files; the release assets contain none of the known private CAD, host or account identifiers. `actionlint` accepted all seven dormant workflows. GitHub Actions are disabled. |
| Reproducibility | Two independent offline builds from the tagged commit produced byte-identical Windows, Linux, wheel, sdist and checksum files. Independent checks accepted both distributions. |
| Release acceptance | The candidate and the downloaded public assets each passed all 6 stages: checksums, both distributions, offline Linux install, wheel/sdist install, byte-identical rebuilds of both examples, and `quickstart --run` qualifying kinematics. The release audit matched all five published entries to the tag and digests. |
| Native SolidWorks | Windows 11 / CPython 3.12.10 / SolidWorks 2026 SP3.2: 275 component instances and 275 mass-property sets, `collectable=True`; upgrade 0.3.22 → 0.3.23 and rollback passed. Doctor, security probes (403/403/404), and the offline first run passed. The isolated rehearsal cleaned up; the production worker stayed on 0.3.22. |
| Public first use | An unauthenticated clone retrieved the tagged source. An unauthenticated Linux bundle download matched `SHA256SUMS`, installed without network dependencies and qualified the example. A separate model repository accepted the public tool commit; model initialization and branch push stayed on its writable remote. |

All hashes below were verified from the published download, not inferred from a successful upload.

| Asset | SHA-256 |
| --- | --- |
| `description-worker-0.3.23-windows-x86_64.zip` | `41f459bf8ae5e963509d521e61855dbf5571b00bdc176a8b70c8615e353ef229` |
| `mimicverse_description-0.3.23-linux-x86_64.zip` | `9a0912901a9a2974b599e3d10ae1d321c64ae765fb307cae7f9c201533ed9719` |
| `mimicverse_description-0.3.23-py3-none-any.whl` | `7454e04ed865407757a0fc1d8f9f9da9425c372625b43f53546cb5a94fd11e29` |
| `mimicverse_description-0.3.23.tar.gz` | `6f54b29ea36742f87dc8f10be1798e64b76825f16fd18ac9402d62dc388a90e2` |
| `SHA256SUMS` | `f34f4b8d0d262f0382a702ade2568149d0ccdfa27b559f4722dde16af96638e2` |

The examples are fixtures, not native CAD model qualifications. Live Onshape acceptance is deferred because the account returns HTTP 402. This tool release does not itself qualify a robot for simulation, training or hardware control. Follow the [Windows first-use guide](solidworks-first-use.en.md) or [runbook](pipeline.en.md) for the model-specific steps.
