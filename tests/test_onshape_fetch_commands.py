"""fetch / fetch-geometry：离线缓存的产出方（配额耗尽后整条链路都依赖它）。"""

import base64
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import cli  # noqa: E402
from tests import onshape_fixture  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onshape"
CACHE = FIXTURES / "cache"
SOURCE = json.loads((CACHE / "source.json").read_text(encoding="utf-8")) if onshape_fixture.AVAILABLE else {}
ELEMENT = SOURCE.get("element_id", "")
PART = "KFzB"
STUDIO, MISSING_STUDIO = onshape_fixture.mass_properties_ids(PART) if onshape_fixture.AVAILABLE else ("", "")


def cube_gltf(size: float = 0.02) -> dict:
    """与几何拆分测试一致的立方体 GLTF（体积/质心可匹配）。"""

    vertices = [
        (0.0, 0.0, 0.0),
        (size, 0.0, 0.0),
        (size, size, 0.0),
        (0.0, size, 0.0),
        (0.0, 0.0, size),
        (size, 0.0, size),
        (size, size, size),
        (0.0, size, size),
    ]
    faces = [
        (0, 3, 2),
        (0, 2, 1),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    positions = b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
    indices = b"".join(struct.pack("<H", index) for face in faces for index in face)
    while len(indices) % 4:
        indices += b"\0"
    blob = positions + indices
    return {
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": len(vertices)},
            {"bufferView": 1, "componentType": 5123, "type": "SCALAR", "count": len(faces) * 3},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)},
        ],
        "buffers": [
            {"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(blob).decode()}
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "nodes": [{"name": "cube", "mesh": 0}],
    }


def seed_cache(folder: Path) -> Path:
    """把夹具 JSON 拷进临时缓存；补齐装配体引用到但夹具未存的第二个 partStudio。"""

    root = Path(folder)
    (root / "json").mkdir(parents=True, exist_ok=True)
    for source in (CACHE / "json").glob("*.json"):
        (root / "json" / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "json" / f"mass_properties_{MISSING_STUDIO}.json").write_text(json.dumps({"bodies": {}}), encoding="utf-8")
    (root / "source.json").write_text(
        json.dumps({"url": f"https://cad.onshape.com/documents/d/w/wid/e/{ELEMENT}", "element_id": ELEMENT}),
        encoding="utf-8",
    )
    return root


class FakeClient:
    """按（临时）缓存快照回答 fetch/fetch-geometry 需要的端点。"""

    def __init__(self, cache_dir: Path, *, gltf: dict | None = None, stl_error: bool = False) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_calls: list[str] = []
        self.gltf = gltf
        self.stl_error = stl_error

    def _json(self, name: str) -> dict:
        return json.loads((self.cache_dir / "json" / name).read_text(encoding="utf-8"))

    def get_assembly(self, ref, configuration="default"):
        return self._json(f"assembly_{ELEMENT}.json")

    def get_assembly_features(self, ref, configuration="default"):
        return self._json(f"assembly_features_{ELEMENT}.json")

    def get_mate_values(self, ref):
        return self._json(f"mate_values_{ELEMENT}.json")

    def get_part_mass_properties(self, ref, element_id, part_id):
        bodies = self._json(f"mass_properties_{element_id}.json")["bodies"]
        return {"bodies": {part_id: bodies[part_id]}}

    def get_studio_mass_properties(self, ref, element_id):
        self.cache_calls.append(element_id)
        return self._json(f"mass_properties_{element_id}.json")

    def get_part_studio_gltf(self, ref, element_id):
        if self.gltf is None:
            from onshape_export.api import OnshapeError

            raise OnshapeError(404, "no gltf")
        return self.gltf

    def get_part_stl(self, ref, element_id, part_id):
        if self.stl_error:
            from onshape_export.api import OnshapeError

            raise OnshapeError(500, "stl failed")
        return (FIXTURES / "model" / "meshes" / "stp39__stp39.stl").read_bytes()


def run_cli(argv) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


@onshape_fixture.requires_fixture
class FetchCommandTests(unittest.TestCase):
    def test_fetch_writes_every_cache_file_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            seed_cache(Path(folder))
            fake = FakeClient(Path(folder))
            with mock.patch.object(cli, "_client", return_value=fake):
                code, output = run_cli(
                    [
                        "fetch",
                        "--cache",
                        folder,
                        "--url",
                        f"https://cad.onshape.com/documents/d/w/wid/e/{ELEMENT}",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0, output)
            summary = json.loads(output)
            self.assertEqual(summary["command"], "fetch")
            self.assertEqual(summary["instances"], 24)
            self.assertTrue((Path(folder) / "source.json").is_file())
            self.assertTrue((Path(folder) / "json" / f"assembly_{ELEMENT}.json").is_file())
            self.assertIn(STUDIO, fake.cache_calls)

    def test_fetch_geometry_writes_gltf_parts_into_the_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = seed_cache(Path(folder))
            with mock.patch.object(cli, "_client", return_value=FakeClient(cache, gltf=cube_gltf())):
                code, output = run_cli(["fetch-geometry", "--cache", str(cache), "--json"])
            # 合成 GLTF 只能匹配到夹具里的一个零件，其余按 unresolved 如实报告
            self.assertIn(code, (0, 1), output)
            report = json.loads(output)
            self.assertEqual(report["command"], "fetch-geometry")
            self.assertGreaterEqual(report["parts_written"], 1)
            written = list((cache / "bytes").glob("stl_*.stl"))
            self.assertTrue(written, "GLTF 拆分结果必须落进缓存")

    def test_fetch_geometry_reports_unresolved_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = seed_cache(Path(folder))
            studios = json.loads((cache / "json" / f"mass_properties_{STUDIO}.json").read_text(encoding="utf-8"))
            body = studios["bodies"][PART]
            studios["bodies"][PART] = {**body, "volume": [999.0], "centroid": [9, 9, 9]}
            (cache / "json" / f"mass_properties_{STUDIO}.json").write_text(json.dumps(studios), encoding="utf-8")
            with mock.patch.object(cli, "_client", return_value=FakeClient(cache, gltf=cube_gltf())):
                code, output = run_cli(["fetch-geometry", "--cache", str(cache), "--json"])
            self.assertEqual(code, cli.EXIT_FINDINGS, output)
            report = json.loads(output)
            self.assertGreaterEqual(report["summary"]["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
