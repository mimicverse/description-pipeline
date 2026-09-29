"""partId / 元素配置的唯一性：碰撞必须 fail closed，作用域读数必须各用各的。"""

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.sources.onshape.errors import IDENTITY_COLLISION, OnshapeSourceError
from description_pipeline.sources.onshape.freeze import freeze, load_scene
from description_pipeline.sources.onshape.normalize import _raw_bodies
from description_pipeline.sources.onshape.verify import _RawEvidence

from . import helpers
from .helpers import PART_A, ROOT_ELEMENT, STUDIO_A, STUDIO_B, URL, write_cache

CAPTURE = {"evidence": "fixture", "reason": "合成夹具", "at": "2026-01-01T00:00:00Z"}


def patch_studio_bodies(cache: Path, element_id: str, bodies: dict) -> None:
    path = cache / "json" / f"mass_properties_{element_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bodies"].update(bodies)
    path.write_text(json.dumps(payload), encoding="utf-8")


class IdentityCollisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-collision-"))
        self.cache = write_cache(self.tmp / "cache")

    def freeze(self, **overrides):
        config = {"url": URL, "cache": str(self.cache), "offline": True, "capture": CAPTURE}
        config.update(overrides)
        return freeze(config, self.tmp / "snapshot")

    def test_cross_studio_part_id_with_geometry_is_rejected(self):
        """两个零件工作室出现同一个 partId 且都有几何：文件名无法唯一 → 拒绝。"""

        patch_studio_bodies(self.cache, STUDIO_B, {PART_A: helpers.body(2.0)})
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, IDENTITY_COLLISION)
        self.assertEqual(caught.exception.detail["part_id"], PART_A)
        self.assertEqual(sorted(caught.exception.detail["elements"]), sorted([STUDIO_A, STUDIO_B]))

    def test_sanitized_filename_collision_is_rejected(self):
        """净化后同名（PART-B 与 PART_B）会让落盘文件互相覆盖 → 拒绝。"""

        patch_studio_bodies(self.cache, STUDIO_B, {"PART-B": helpers.body(0.2), "PART_B": helpers.body(0.3)})
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, IDENTITY_COLLISION)
        self.assertEqual(caught.exception.detail["name"], helpers.safe_name("PART-B"))
        colliding = caught.exception.detail["part_ids"]
        self.assertIn("PART-B", colliding)
        self.assertEqual({helpers.safe_name(item) for item in colliding}, {helpers.safe_name("PART-B")})

    def test_same_element_with_two_configurations_is_rejected(self):
        """缓存名不含配置：同一元素被两个配置引用时必须拒绝，而不是 last-wins。"""

        payload = json.loads((self.cache / "json" / f"assembly_{ROOT_ELEMENT}.json").read_text(encoding="utf-8"))
        nested = payload["subAssemblies"][0]["instances"][0]
        self.assertEqual(nested["elementId"], STUDIO_A)
        nested["configuration"] = "alt"
        (self.cache / "json" / f"assembly_{ROOT_ELEMENT}.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, IDENTITY_COLLISION)
        # 依赖配置与请求不一致时在入队阶段就失败（detail 形如 {element_id, configuration}）
        self.assertIn("alt", str(caught.exception.detail))


class ScopedReadingTests(unittest.TestCase):
    """跨工作室同名零件：生成侧与 oracle 都必须按 (element, partId) 各取各的读数。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-scoped-"))
        self.cache = write_cache(self.tmp / "cache", stl_parts=(helpers.PART_L, helpers.PART_B))
        # studio B 复用 studio A 的 partId，但质量/体积完全不同
        patch_studio_bodies(self.cache, STUDIO_B, {PART_A: helpers.body(2.0, centroid=(1.0, 0.0, 0.0))})
        freeze(
            {"url": URL, "cache": str(self.cache), "offline": True, "capture": CAPTURE},
            self.tmp / "snapshot",
        )
        self.snapshot = self.tmp / "snapshot"
        self.scene = load_scene(self.snapshot)

    def test_raw_bodies_are_scoped_by_element(self):
        bodies = _raw_bodies(self.snapshot)
        self.assertEqual(bodies[STUDIO_A][PART_A]["mass"][0], 1.5)
        self.assertEqual(bodies[STUDIO_B][PART_A]["mass"][0], 2.0)
        # 扁平读取会把其中一个覆盖掉；这里显式证明两个作用域各自可查
        self.assertNotEqual(bodies[STUDIO_A][PART_A], bodies[STUDIO_B][PART_A])

    def test_oracle_reads_the_same_scope(self):
        raw = _RawEvidence(self.scene, self.snapshot)
        first = raw.body_for(STUDIO_A, PART_A) or {}
        second = raw.body_for(STUDIO_B, PART_A) or {}
        self.assertEqual(first["mass"][0], 1.5)
        self.assertEqual(second["mass"][0], 2.0)
        self.assertEqual(raw.body_conflicts, [])


if __name__ == "__main__":
    unittest.main()
