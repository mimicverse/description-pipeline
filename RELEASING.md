# Release procedure

The public release is the tool and its specifications. Native robot/CAD history
stays in the private model repository. GitHub CI is not part of the release gate.

1. Run the maintained regression suite and static checks with the pinned build
   environment. Test meaningful malformed inputs and semantic mutations after
   resealing; hash mismatch alone is insufficient proof of a quality rule.
2. Capture the neutral moving-joint analytic CAD fixture on Windows. Verify
   native transforms, shaft identity, off-diagonal inertia and full assembly
   closure. Retain actual API, scope, software version and error evidence.
3. Build wheel and source distributions from a clean committed checkout. Embed
   the source commit and code digest; verify installed CLI and package identity
   outside that checkout. Build twice and compare distribution bytes.
4. Rehearse the installed tool on Windows through capture → verify → PR and on
   Linux through frozen check → rebuild → submit. Repeat submission and confirm
   one PR. A bad input or artifact must produce no publication.
5. Deploy the tested Airflow environment and Windows endpoint. Exercise the
   actual DAG against the native endpoint, including retry and failure cases.
6. Audit documentation against command help, schemas, tests and measured
   behavior. Record limitations without implying physical/control qualification.
7. Push the reviewed implementation to public `main`, tag `v1.0.0`, and publish
   the distributions, SHA-256 manifest and acceptance evidence. Verify remote
   commit, tag, release assets and installed version.

Completion requires all seven steps. Unit tests, native capture alone, a draft
PR or a documentation-only deployment do not establish release readiness.
