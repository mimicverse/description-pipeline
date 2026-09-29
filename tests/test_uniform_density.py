"""独立 oracle：声明了 uniform_density_visual 的 link 必须有几何结论。"""

import math
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from description_pipeline.verification import uniform_density as ud


def binary_stl(triangles) -> bytes:
    payload = b"uniform-density-oracle".ljust(80, b"\0") + struct.pack("<I", len(triangles))
    for corners in triangles:
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for corner in corners:
            payload += struct.pack("<3f", *corner)
        payload += struct.pack("<H", 0)
    return payload


def cube_stl(size: float) -> bytes:
    """水密立方体（12 个三角面）。"""

    h = size / 2.0
    v = [
        (-h, -h, -h),
        (h, -h, -h),
        (h, h, -h),
        (-h, h, -h),
        (-h, -h, h),
        (h, -h, h),
        (h, h, h),
        (-h, h, h),
    ]
    faces = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (2, 3, 7),
        (2, 7, 6),
        (1, 2, 6),
        (1, 6, 5),
        (0, 4, 7),
        (0, 7, 3),
    ]
    return binary_stl([(v[a], v[b], v[c]) for a, b, c in faces])


def box_inertia(size, mass):
    x, y, z = size
    return [mass * (y * y + z * z) / 12.0, 0.0, 0.0, mass * (x * x + z * z) / 12.0, 0.0, mass * (x * x + y * y) / 12.0]


def link(name, visuals, inertia, *, mass=1.0, claimed=True, rpy=(0.0, 0.0, 0.0), xyz=(0.0, 0.0, 0.0), provenance=None):
    payload = {"inertia_model": ud.CLAIM} if claimed else {}
    payload.update(provenance or {})
    return {
        "id": name,
        "name": name,
        "visuals": visuals,
        "collisions": [],
        "inertial": {"mass": mass, "xyz": list(xyz), "rpy": list(rpy), "inertia": inertia},
        "provenance": payload,
    }


def box_visual(size, xyz=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    return {"kind": "box", "size": list(size), "xyz": list(xyz), "rpy": list(rpy), "scale": list(scale)}


def mesh_visual(filename="meshes/cube.stl", xyz=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    return {"kind": "mesh", "filename": filename, "xyz": list(xyz), "rpy": list(rpy), "scale": list(scale)}


class PrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uniform-density-"))

    def evidence(self, links, profile=None):
        return ud.uniform_density_evidence(self.tmp, {"links": links}, profile or {})

    def test_box_with_correct_inertia_passes(self):
        size = (0.05, 0.05, 0.05)
        results = self.evidence([link("box", [box_visual(size)], box_inertia(size, 1.0))])
        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(results[0]["volume_m3"], 0.05**3, places=12)
        self.assertAlmostEqual(results[0]["density_kg_m3"], 1.0 / 0.05**3, places=6)
        self.assertAlmostEqual(results[0]["tensor_error"], 0.0, places=12)

    def test_box_with_wrong_inertia_fails(self):
        size = (0.05, 0.05, 0.05)
        wrong = [value * 3.0 for value in box_inertia(size, 1.0)]
        results = self.evidence([link("box", [box_visual(size)], wrong)])
        self.assertEqual(results[0]["status"], "failed")
        self.assertGreater(results[0]["tensor_error"], results[0]["tolerances"]["tensor_tolerance"])

    def test_uniform_scaled_box_uses_scaled_dimensions(self):
        size = (0.05, 0.05, 0.05)
        scale = 2.0
        scaled = tuple(value * scale for value in size)
        results = self.evidence(
            [link("box", [box_visual(size, scale=(scale, scale, scale))], box_inertia(scaled, 1.0))]
        )
        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(results[0]["volume_m3"], 0.1**3, places=12)

    def test_non_uniform_scaled_primitive_is_not_run(self):
        visual = box_visual((0.05, 0.05, 0.05), scale=(2.0, 1.0, 1.0))
        results = self.evidence([link("box", [visual], box_inertia((0.05, 0.05, 0.05), 1.0))])
        self.assertEqual(results[0]["status"], "not_run")
        self.assertIn("非均匀缩放", results[0]["reason"])

    def test_sphere_and_cylinder_match_analytic_inertia(self):
        radius, length = 0.03, 0.1
        sphere_i = 2.0 / 5.0 * radius**2
        axial = 0.5 * radius**2
        radial = radius**2 / 4.0 + length**2 / 12.0
        results = self.evidence(
            [
                link(
                    "sphere",
                    [{"kind": "sphere", "radius": radius, "xyz": [0, 0, 0], "rpy": [0, 0, 0], "scale": [1, 1, 1]}],
                    [sphere_i, 0, 0, sphere_i, 0, sphere_i],
                ),
                link(
                    "cyl",
                    [
                        {
                            "kind": "cylinder",
                            "radius": radius,
                            "length": length,
                            "xyz": [0, 0, 0],
                            "rpy": [0, 0, 0],
                            "scale": [1, 1, 1],
                        }
                    ],
                    [radial, 0, 0, radial, 0, axial],
                ),
            ]
        )
        self.assertEqual([item["status"] for item in results], ["passed", "passed"])

    def test_translated_box_checks_com(self):
        size = (0.04, 0.06, 0.08)
        offset = (0.5, 0.0, 0.0)
        item = link("box", [box_visual(size, xyz=offset)], box_inertia(size, 1.0), xyz=offset)
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(results[0]["com_error_m"], 0.0, places=12)

    def test_wrong_com_is_caught(self):
        size = (0.04, 0.06, 0.08)
        item = link("box", [box_visual(size, xyz=(0.5, 0.0, 0.0))], box_inertia(size, 1.0), xyz=(0.0, 0.0, 0.0))
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "failed")
        self.assertGreater(results[0]["com_error_m"], results[0]["tolerances"]["uniform_density_com_atol_m"])


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uniform-density-mesh-"))

    def write_mesh(self, size: float, name: str = "meshes/cube.stl") -> None:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cube_stl(size))

    def evidence(self, links, profile=None):
        return ud.uniform_density_evidence(self.tmp, {"links": links}, profile or {})

    def test_watertight_mesh_matches_uniform_density_inertia(self):
        size = 0.05
        self.write_mesh(size)
        results = self.evidence([link("cube", [mesh_visual()], box_inertia((size, size, size), 1.0))])
        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(results[0]["volume_m3"] / size**3, 1.0, places=6)

    def test_non_uniform_mesh_scale_is_applied(self):
        """x' = R·S·x：体积按 det(S)、二阶矩按 det(S)·S μ Sᵀ 缩放。"""

        size = 0.05
        self.write_mesh(size)
        scale = (2.0, 3.0, 4.0)
        scaled = tuple(value * factor for value, factor in zip((size, size, size), scale, strict=True))
        results = self.evidence([link("cube", [mesh_visual(scale=scale)], box_inertia(scaled, 1.0))])
        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(results[0]["density_kg_m3"], 1.0 / (size**3 * 24.0), places=3)

    def test_second_moment_is_not_mistaken_for_inertia(self):
        size = 0.05
        self.write_mesh(size)
        volume = size**3
        mu = volume * size**2 / 12.0  # μ 只有正确惯量的一半（立方体）
        wrong = [mu, 0.0, 0.0, mu, 0.0, mu]
        results = self.evidence([link("cube", [mesh_visual()], wrong)])
        self.assertEqual(results[0]["status"], "failed")

    def test_open_mesh_is_not_run_instead_of_silently_skipped(self):
        path = self.tmp / "meshes/open.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary_stl([((-0.01, 0.0, 0.0), (0.01, 0.0, 0.0), (0.0, 0.01, 0.0))]))
        results = self.evidence([link("open", [mesh_visual("meshes/open.stl")], [1e-6, 0, 0, 1e-6, 0, 1e-6])])
        self.assertEqual(results[0]["status"], "not_run")
        self.assertIn("水密", results[0]["reason"])

    def test_missing_mesh_file_is_not_run(self):
        results = self.evidence([link("gone", [mesh_visual("meshes/missing.stl")], [1e-6, 0, 0, 1e-6, 0, 1e-6])])
        self.assertEqual(results[0]["status"], "not_run")


class TensorExpressionTests(unittest.TestCase):
    """惯量表达系、完整张量与近重根：旧的主惯量比/主轴判据会误判的用例。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uniform-density-tensor-"))

    def evidence(self, links, profile=None):
        return ud.uniform_density_evidence(self.tmp, {"links": links}, profile or {})

    def test_inertial_rpy_rotates_tensor_into_link_frame(self):
        """模型存的是惯量系下的张量；同一物理刚体换个表达系仍必须通过。"""

        size = (0.04, 0.06, 0.08)
        rpy = (0.0, 0.0, math.pi / 2.0)
        rotation = ud._rotation(rpy)
        link_tensor = np.diag(np.asarray(box_inertia(size, 1.0))[[0, 3, 5]])
        expressed = rotation.T @ link_tensor @ rotation  # I_inertial = Rᵀ·I_link·R
        item = link("box", [box_visual(size)], expressed.flatten()[[0, 1, 2, 4, 5, 8]].tolist(), rpy=rpy)
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "passed")
        self.assertTrue(np.allclose(results[0]["model_tensor"], link_tensor, atol=1e-15))

    def test_wrong_inertial_frame_declaration_fails(self):
        """张量内容对但表达系写错（或反之）都必须失败。"""

        size = (0.04, 0.06, 0.08)
        correct = np.diag(np.asarray(box_inertia(size, 1.0))[[0, 3, 5]])
        six = correct.flatten()[[0, 1, 2, 4, 5, 8]].tolist()
        wrong_frame = link("box", [box_visual(size)], six, rpy=(0.0, 0.0, math.pi / 4.0))
        already_rotated = link(
            "box",
            [box_visual(size)],
            (ud._rotation((0.0, 0.0, math.pi / 4.0)) @ correct @ ud._rotation((0.0, 0.0, math.pi / 4.0)).T)
            .flatten()[[0, 1, 2, 4, 5, 8]]
            .tolist(),
            rpy=(0.0, 0.0, 0.0),
        )
        results = self.evidence([wrong_frame, already_rotated])
        self.assertEqual([item["status"] for item in results], ["failed", "failed"])

    def test_same_largest_eigenvalue_but_wrong_tensor_fails(self):
        """最大主惯量相同、其余分量错：旧判据会漏，新判据必须抓到。"""

        size = (0.04, 0.06, 0.08)
        expected = np.diag(np.asarray(box_inertia(size, 1.0))[[0, 3, 5]])
        near_degenerate = np.diag([expected[0][0], expected[0][0], expected[2][2] * 0.8])
        item = link("box", [box_visual(size)], near_degenerate.flatten()[[0, 1, 2, 4, 5, 8]].tolist())
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "failed")
        self.assertLessEqual(max(np.linalg.eigvalsh(near_degenerate)), max(np.linalg.eigvalsh(expected)) + 1e-18)

    def test_near_degenerate_eigenvalues_do_not_cause_spurious_failure(self):
        size = (0.04, 0.06, 0.08)
        expected = np.diag(np.asarray(box_inertia(size, 1.0))[[0, 3, 5]])
        perturbed = expected.copy()
        perturbed[0, 0] *= 1.0 + 1e-5
        perturbed[1, 1] *= 1.0 - 1e-5
        item = link("box", [box_visual(size)], perturbed.flatten()[[0, 1, 2, 4, 5, 8]].tolist())
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "passed")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uniform-density-policy-"))

    def evidence(self, links, profile=None):
        return ud.uniform_density_evidence(self.tmp, {"links": links}, profile or {})

    def test_unclaimed_link_is_not_applicable(self):
        size = (0.05, 0.05, 0.05)
        results = self.evidence([link("plain", [box_visual(size)], box_inertia(size, 1.0), claimed=False)])
        self.assertEqual(results[0]["status"], "not_applicable")

    def test_claimed_link_without_geometry_is_not_run(self):
        results = self.evidence([link("empty", [], [1e-6, 0, 0, 1e-6, 0, 1e-6])])
        self.assertEqual(results[0]["status"], "not_run")
        self.assertIn("没有可视几何", results[0]["reason"])

    def test_massless_claim_is_not_run(self):
        size = (0.05, 0.05, 0.05)
        results = self.evidence([link("light", [box_visual(size)], [0, 0, 0, 0, 0, 0], mass=0.0)])
        self.assertEqual(results[0]["status"], "not_run")
        self.assertIn("正质量", results[0]["reason"])

    def test_overlapping_visuals_require_explicit_disjoint_declaration(self):
        size = (0.05, 0.05, 0.05)
        visuals = [box_visual(size), box_visual(size, xyz=(0.01, 0.0, 0.0))]
        results = self.evidence([link("stack", visuals, box_inertia(size, 1.0))])
        self.assertEqual(results[0]["status"], "not_run")
        self.assertIn("重叠", results[0]["reason"])

    def test_declared_disjoint_visuals_are_summed(self):
        size = (0.05, 0.05, 0.05)
        offset = (0.1, 0.0, 0.0)
        visuals = [box_visual(size), box_visual(size, xyz=offset)]
        half = np.diag(np.asarray(box_inertia(size, 0.5))[[0, 3, 5]])
        distance = offset[0] / 2.0
        parallel = 0.5 * distance**2
        # 平行轴只作用在垂直于 x 的两个方向上
        expected = 2.0 * half + np.diag([0.0, 2.0 * parallel, 2.0 * parallel])
        item = link(
            "pair",
            visuals,
            expected.flatten()[[0, 1, 2, 4, 5, 8]].tolist(),
            xyz=(distance, 0.0, 0.0),
            provenance={ud.OVERLAP_DECLARATION: ud.DISJOINT},
        )
        results = self.evidence([item])
        self.assertEqual(results[0]["status"], "passed", results[0])
        self.assertAlmostEqual(results[0]["volume_m3"], 2.0 * size[0] * size[1] * size[2], places=12)

    def test_profile_overrides_are_reported(self):
        size = (0.05, 0.05, 0.05)
        wrong = [value * 1.2 for value in box_inertia(size, 1.0)]
        strict = self.evidence(
            [link("box", [box_visual(size)], wrong)],
            {"uniform_density_rtol": 1e-6, "inertia_atol": 1e-12},
        )
        relaxed = self.evidence(
            [link("box", [box_visual(size)], wrong)],
            {"uniform_density_rtol": 0.5, "inertia_atol": 1e-12},
        )
        self.assertEqual(strict[0]["status"], "failed")
        self.assertEqual(relaxed[0]["status"], "passed")
        self.assertEqual(strict[0]["tolerances"]["uniform_density_rtol"], 1e-6)
        self.assertEqual(relaxed[0]["tolerances"]["uniform_density_rtol"], 0.5)

    def test_summary_does_not_count_not_run_as_ok(self):
        size = (0.05, 0.05, 0.05)
        evidence = self.evidence(
            [
                link("good", [box_visual(size)], box_inertia(size, 1.0)),
                link("bad", [box_visual(size)], [value * 4.0 for value in box_inertia(size, 1.0)]),
                link("plain", [box_visual(size)], box_inertia(size, 1.0), claimed=False),
                link("empty", [], [1e-6, 0, 0, 1e-6, 0, 1e-6]),
            ]
        )
        summary = ud.summarize(evidence)
        self.assertEqual(summary["passed"], ["good"])
        self.assertEqual(summary["failed"], ["bad"])
        self.assertEqual(summary["not_run"], ["empty"])
        self.assertEqual(summary["not_applicable"], ["plain"])
        self.assertFalse(summary["ok"])

    def test_summary_ok_requires_every_applicable_link_passed(self):
        size = (0.05, 0.05, 0.05)
        evidence = self.evidence(
            [
                link("good", [box_visual(size)], box_inertia(size, 1.0)),
                link("plain", [box_visual(size)], box_inertia(size, 1.0), claimed=False),
            ]
        )
        summary = ud.summarize(evidence)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["applicable"], ["good"])


if __name__ == "__main__":
    unittest.main()
