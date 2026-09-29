import copy
import unittest

from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import ConfigError

from .helpers import make_config_dict


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        cfg = config_from_dict(make_config_dict())
        self.assertEqual(cfg.model, "fake_robot_v1")
        self.assertEqual(cfg.notes.get("root_link"), "base_link")
        assert cfg.joints[0].axis is not None
        self.assertAlmostEqual(cfg.joints[0].axis[2], 1.0)

    def test_duplicate_link_names(self):
        data = make_config_dict()
        data["links"][1]["name"] = "base_link"
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_joint_parent_missing(self):
        data = make_config_dict()
        data["joints"][0]["parent"] = "nope"
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_movable_joint_requires_limits(self):
        data = make_config_dict()
        del data["joints"][0]["limits"]
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_limits_must_be_ordered(self):
        data = make_config_dict()
        data["joints"][0]["limits"]["lower"] = 2.0
        data["joints"][0]["limits"]["upper"] = 1.0
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_component_assigned_twice(self):
        data = make_config_dict()
        data["links"][1]["components"] = ["Base-1"]
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_axis_is_normalized(self):
        data = make_config_dict()
        data["joints"][0]["axis"] = [0.0, 0.0, 2.0]
        cfg = config_from_dict(data)
        assert cfg.joints[0].axis is not None
        self.assertEqual(tuple(cfg.joints[0].axis), (0.0, 0.0, 1.0))

    def test_zero_axis_rejected(self):
        data = make_config_dict()
        data["joints"][0]["axis"] = [0.0, 0.0, 0.0]
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_all_movable_types_require_explicit_axis(self):
        for kind in ("revolute", "continuous", "prismatic"):
            for missing in (True, False):
                with self.subTest(kind=kind, missing=missing):
                    data = make_config_dict()
                    data["joints"][0]["type"] = kind
                    if missing:
                        del data["joints"][0]["axis"]
                    else:
                        data["joints"][0]["axis"] = None
                    with self.assertRaisesRegex(ConfigError, "explicit axis"):
                        config_from_dict(data)

    def test_nonfinite_and_overflow_axes_rejected(self):
        for axis in ([float("inf"), 0, 1], [float("nan"), 0, 1], [0, -float("inf"), 1], [1.7e308] * 3, [True, 0, 1]):
            with self.subTest(axis=axis):
                data = make_config_dict()
                data["joints"][0]["axis"] = axis
                with self.assertRaises(ConfigError):
                    config_from_dict(data)

    def test_large_finite_axis_normalizes_without_squaring_overflow(self):
        data = make_config_dict()
        data["joints"][0]["axis"] = [1e200, 0, 1e200]
        cfg = config_from_dict(data)
        assert cfg.joints[0].axis is not None
        self.assertAlmostEqual(cfg.joints[0].axis[0], 2**-0.5)

    def test_fixed_joint_can_omit_axis(self):
        data = make_config_dict()
        data["joints"][0]["type"] = "fixed"
        del data["joints"][0]["axis"]
        self.assertIsNone(config_from_dict(data).joints[0].axis)

    def test_two_parents_rejected(self):
        data = make_config_dict()
        extra = copy.deepcopy(data["joints"][0])
        extra["name"] = "fix_extra"
        extra["type"] = "fixed"
        extra.pop("limits")
        extra.pop("dynamics")
        extra.pop("coordinate_system")
        data["joints"].append(extra)
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_scalar_joint_kind_rejected(self):
        data = make_config_dict()
        data["joints"][0]["type"] = "ball"
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_coordinate_system_override_shape(self):
        data = make_config_dict()
        data["coordinate_system_transforms"] = {"CS_dof_arm": [0.0] * 15}
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_joint_limit_must_be_a_finite_number(self):
        """A string or NaN limit is a config error, never a crash."""
        from tools.solidworks_export.config import config_from_dict
        from tools.solidworks_export.errors import ConfigError

        for bad in ("abc", None, float("nan"), float("inf")):
            data = make_config_dict()
            data["joints"][0]["limits"]["lower"] = bad
            with self.assertRaises(ConfigError):
                config_from_dict(data)


class ValidateConfigCommandTests(unittest.TestCase):
    """``swctl validate-config`` checks a config offline, before any CAD access."""

    def run_cli(self, path):
        import contextlib
        import io
        import json as _json

        from tools.solidworks_export import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["validate-config", str(path)])
        payload = _json.loads((out.getvalue() or err.getvalue()).strip())
        return code, payload

    def test_valid_config_reports_a_summary(self):
        import json as _json
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "export_config.json"
        path.write_text(_json.dumps(make_config_dict()), encoding="utf-8")
        code, payload = self.run_cli(path)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["links"], 2)
        self.assertEqual(payload["joints"], 1)
        self.assertEqual(payload["frames"], 0)

    def test_invalid_config_fails_with_an_error_code(self):
        import json as _json
        import tempfile
        from pathlib import Path

        data = make_config_dict()
        data["joints"][0]["type"] = "bogus"
        path = Path(tempfile.mkdtemp()) / "export_config.json"
        path.write_text(_json.dumps(data), encoding="utf-8")
        code, payload = self.run_cli(path)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_config")


if __name__ == "__main__":
    unittest.main()
