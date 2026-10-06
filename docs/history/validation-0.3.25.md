# 0.3.25 validation (unreleased)

0.3.25 is **not published or tagged**. This record holds the release-gate history until a fresh
native rehearsal and the exact-commit acceptance pass; the final validation record replaces it at
publication.

| Gate | State |
| --- | --- |
| Linux source quality | Passed locally: six gates, 1,298 tests, 113 skips. |
| Fresh native Windows rehearsal | **Not passed.** Earlier fresh rehearsals failed when Windows App Control rejected MuJoCo loading (`WinError 4551`). A fresh package has not completed installation, Doctor, capture and the consumer checks. |
| App Control classifier | The rejection is now classified precisely (localized `ctypes.FormatError(4551)` text, Windows-only message matching, exception chains); Doctor tests cover it and the classifier was checked with a zh-CN message. This is a diagnostic improvement only. |
| Preserved runtime probe | The old 025u runtime imported MuJoCo and `_callbacks` successfully without a policy change; no cause is inferred from that probe. It is not a fresh-install rehearsal. |
| Exact-commit acceptance | Not run against a 0.3.25 bundle. No tag and no published assets. |
| Release gate | A fresh native Windows rehearsal on the candidate bundle (installation, Doctor, capture and the consumer checks) followed by the exact-commit acceptance. A diagnostic improvement, a preserved runtime probe, generated files or a submitted pull request do not satisfy the gate. |

This release does not itself qualify any robot for simulation, training, hardware or mechanical
acceptance. Follow the [Windows first-use guide](../solidworks-first-use.en.md) or the
[pipeline runbook](../pipeline.en.md) for model-specific steps.
