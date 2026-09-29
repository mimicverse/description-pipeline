# Examples

| Example | What it shows |
| --- | --- |
| [`demo-arm/`](demo-arm/) | A complete two-joint arm workspace that runs offline from a fixture snapshot: freeze → build → check, with the generated URDF/MJCF and the qualification report. |
| [`mesh-arm/`](mesh-arm/) | The same offline flow with binary STL meshes and the uniform-density inertia oracle instead of primitive geometry. |

Examples are tooling artifacts. They are explicitly labelled fixture evidence, never claim CAD
provenance, and exist so the pipeline can be exercised without a licensed CAD workstation. Real
robots live on their own `feature/<hardware>` and `release/<hardware>` branches.
