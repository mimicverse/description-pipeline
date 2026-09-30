# 0.3.24 validation

Published on 2026-09-29 as [v0.3.24](https://github.com/mimicverse/description-pipeline/releases/tag/v0.3.24). The tag pins source commit `f7db49485e285b4f79b06cd80a3bb304066de54d`; both offline bundles bind package digest `38be63b0e266da9925efe0a268220d1c2691b3e26ec7df5469d1139b98ca2c18`. The private model/CAD repository remains private.

| Gate | Result |
| --- | --- |
| Source quality | 6/6 local steps; 1,232 tests (`skipped=113`), including Windows type checking. GitHub Actions were not used. |
| Dependencies and source | No known advisories in the 27 Linux, 29 Windows development, or 17 Windows runtime pins. `gitleaks` found no leaks in 11 public commits; `actionlint` accepted the dormant workflows. |
| Reproducibility | Two offline builds from the tagged commit produced byte-identical Windows and Linux bundles, wheel, sdist, and `SHA256SUMS`. Both distributions passed independent checks. |
| Release acceptance | Candidate artifacts and independently downloaded public assets each passed all six stages: checksums, distribution verification, offline Linux installation, wheel/sdist installation, byte-identical example rebuilds, and a qualified offline first run. |
| Native Windows | SolidWorks 34.0.0: 361 components and 361 mass-property readings, `collectable=True`. Upgrade 0.3.23 → 0.3.24 and rollback passed; worker guards returned 403/403/404, and the offline first run qualified for kinematics. |
| Native model test | The packaged 0.3.24 runtime froze an isolated M3.0 assembly and then built and checked it with the 25-name structural joint ledger required by `URDF208`. Snapshot `4ab4ed33b1cc7e60e419f2c07213b265aa2843f5304c4f1e389696b975fce240` and bundle subject `8ce495897c19da6bb70cc28aeee196a39f07fdaa65519d4296ea55cebeb420a6` matched their manifests; all 78 checks passed with `qualified_for: ["kinematics"]` and no blockers. The assembly-versus-leaf mass difference was reported as an advisory, as designed. |

All hashes below were checked against the downloaded public release, not inferred from a successful upload.

| Asset | SHA-256 |
| --- | --- |
| `description-worker-0.3.24-windows-x86_64.zip` | `6b789db9e3c065d44da347ca767b9033c6d4dd21fd60f74453e4efd71b4a6802` |
| `mimicverse_description-0.3.24-linux-x86_64.zip` | `9d7e119cb1a859b59435ef04e10ea4b3f319fe9b47fa547dd7e84c8044431a37` |
| `mimicverse_description-0.3.24-py3-none-any.whl` | `de044ddba39b3394b6bbe4d19e24eb2fa58ba1f932860d061a4a571a217b8200` |
| `mimicverse_description-0.3.24.tar.gz` | `6f3ad851cb33d95bf28bedc172da101a4b7cf3133feb91f95c93d3de67a55573` |
| `SHA256SUMS` | `a858099376d06f6efcf1d85440daf147a34c096c57348498b09a50ca459863f6` |

The Windows PowerShell archive extractor failed when an unusually long custom install root pushed one bundle member past the legacy path limit; a short install root completed the same install. The M3.0 model test needed an explicit joint ledger because its older locked tool did not require one. Live Onshape validation remains deferred by the account's HTTP 402 quota response. This tool release does not itself qualify any robot for simulation, training, or hardware control.
