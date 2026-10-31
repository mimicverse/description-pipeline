"""Numerical and adversarial checks at the URDF-to-MJCF projection boundary."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import mujoco
import numpy as np

from description_pipeline import mjcf
from description_pipeline import mjcf_contract as contract
from description_pipeline.io import PipelineError, inventory
from description_pipeline.verification.mjcf import evaluate_projection


def tetrahedron() -> bytes:
    vertices = [(0, 0, 0), (0.03, 0, 0), (0, 0.02, 0), (0, 0, 0.04)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    result = bytearray(80) + struct.pack('<I', len(faces))
    for face in faces:
        result += struct.pack('<12fH', 0, 0, 0, *(value for index in face for value in vertices[index]), 0)
    return bytes(result)


SOURCE = '''<robot name="neutral_projection">
<!--WARNING native.test: Preserved source scope. -->
<link name="base">
  <inertial><origin xyz="0.001 0.002 0.003" rpy="0.2 -0.3 0.4"/>
    <mass value="1"/><inertia ixx="0.04" iyy="0.05" izz="0.06" ixy="0.001" ixz="0.002" iyz="0.0015"/></inertial>
  <visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry><mesh filename="../meshes/part.stl"/></geometry>
    <material name="blue"><color rgba="0.1 0.3 0.8 1"/></material></visual>
  <visual><origin xyz="0.02 0.01 0.03" rpy="0.1 0.2 -0.3"/>
    <geometry><mesh filename="../meshes/part.stl" scale="1.2 0.8 1.1"/></geometry></visual>
  <collision><geometry><box size="0.02 0.03 0.04"/></geometry></collision>
</link>
<link name="arm"><inertial><origin xyz="0.01 0 0.03" rpy="0 0.2 0"/>
  <mass value="0.5"/><inertia ixx="0.003" iyy="0.004" izz="0.005" ixy="0.0001" ixz="0.0002" iyz="0.0001"/>
</inertial><visual><geometry><box size="0.01 0.02 0.06"/></geometry></visual></link>
<link name="tool_frame"/>
<joint name="motion" type="revolute"><parent link="base"/><child link="arm"/>
  <origin xyz="0.02 0 0.05" rpy="0.1 -0.2 0.3"/><axis xyz="1 2 -3"/>
  <limit lower="-0.7" upper="1.2" effort="0" velocity="0"/>
</joint>
<joint name="tool_mount" type="fixed"><parent link="arm"/><child link="tool_frame"/>
  <origin xyz="0 0 0.06" rpy="0 0 0"/></joint>
</robot>'''


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mjcf-projection-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'urdf').mkdir()
        (self.root/'meshes').mkdir()
        (self.root/contract.URDF_NAME).write_text(SOURCE)
        (self.root/'meshes/part.stl').write_bytes(tetrahedron())

    def binding(self):
        files = {}
        for directory in ('urdf', 'meshes', 'mjcf'):
            for name in inventory(self.root/directory):
                path = self.root/directory/name
                raw = path.read_bytes()
                files[f'{directory}/{name}'] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        return {'schema_version': contract.INPUT_BINDING_SCHEMA,
                'source': {'urdf': contract.URDF_NAME, 'meshes': ['meshes/part.stl']},
                'files': files, 'tool': contract.tool_identity(), 'engine': contract.engine_identity()}

    def build(self, config=None):
        mjcf.project(self.root, json.dumps(config or {}).encode())
        return evaluate_projection(self.root, self.binding())

    def mutate(self, path, attribute, value):
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        tree = ET.parse(self.root/contract.ROBOT_NAME, parser=parser)
        tree.find(path).set(attribute, value)
        tree.write(self.root/contract.ROBOT_NAME)
        return evaluate_projection(self.root, self.binding())

    def test_complete_independent_projection_and_source_stability(self):
        before = inventory(self.root)
        report = self.build()
        self.assertTrue(report['passed'], report['checks'])
        self.assertEqual(1, report['facts']['collision_geoms'])
        for name, sha in before.items():
            self.assertEqual(sha, inventory(self.root)[name])
        model = mujoco.MjModel.from_xml_path(str(self.root/contract.ROBOT_NAME))
        self.assertEqual(0, model.nu)
        self.assertEqual(3, model.nbody-1)
        self.assertAlmostEqual(1.5, float(model.body_mass.sum()))
        self.assertEqual({'bytes': 2, 'sha256': hashlib.sha256(b'{}').hexdigest()},
                         self.binding()['files'][contract.CONFIG_NAME])

    def test_signed_axis_mutation_is_rejected_after_valid_rebinding(self):
        self.assertTrue(self.build()['passed'])
        report = self.mutate('.//joint[@name="motion"]', 'axis', '-1 -2 3')
        failed = {row['id'] for row in report['checks'] if not row['passed']}
        self.assertFalse(report['passed'])
        self.assertTrue({'kinematics.axes', 'kinematics.fk'} & failed, report['checks'])

    def test_inertia_mutation_is_rejected_after_valid_rebinding(self):
        self.assertTrue(self.build()['passed'])
        report = self.mutate('.//body[@name="arm"]/inertial', 'mass', '0.7')
        self.assertFalse(report['passed'])
        self.assertIn('inertia.links', {row['id'] for row in report['checks'] if not row['passed']})

    def test_source_warning_loss_is_rejected_after_valid_rebinding(self):
        self.assertTrue(self.build()['passed'])
        path = self.root/contract.ROBOT_NAME
        original = b'WARNING native.test: Preserved source scope. '
        path.write_bytes(path.read_bytes().replace(original, b'Removed warning'))
        report = evaluate_projection(self.root, self.binding())
        self.assertFalse(report['passed'])
        self.assertIn('provenance.warnings', {row['id'] for row in report['checks'] if not row['passed']})

    def test_extra_xml_resource_is_rejected_before_engine_loading(self):
        self.assertTrue(self.build()['passed'])
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        tree = ET.parse(self.root/contract.ROBOT_NAME, parser=parser)
        ET.SubElement(tree.getroot(), 'include', file='../unsealed.xml')
        tree.write(self.root/contract.ROBOT_NAME)
        report = evaluate_projection(self.root, self.binding())
        self.assertFalse(report['passed'])
        engine = next(row for row in report['checks'] if row['id'] == 'engine.load')
        self.assertFalse(engine['passed'])
        self.assertIn('unsupported', engine['details']['error'])

    def test_existing_output_is_preserved(self):
        self.assertTrue(self.build()['passed'])
        before = inventory(self.root)
        with self.assertRaises(FileExistsError):
            mjcf.project(self.root)
        self.assertEqual(before, inventory(self.root))

    def test_different_absolute_paths_produce_identical_new_bytes(self):
        other = self.root/'other'
        for directory in ('urdf', 'meshes'):
            shutil.copytree(self.root/directory, other/directory)
        mjcf.project(self.root)
        mjcf.project(other)
        self.assertEqual(inventory(self.root/'mjcf'), inventory(other/'mjcf'))

    def test_continuous_and_prismatic_joint_semantics(self):
        path = self.root/contract.URDF_NAME
        for kind in ('continuous', 'prismatic'):
            with self.subTest(kind=kind):
                path.write_text(SOURCE.replace('type="revolute"', f'type="{kind}"'))
                report = self.build({'base': 'fixed'})
                self.assertTrue(report['passed'], report['checks'])
                shutil.rmtree(self.root/'mjcf')

    def test_nonfinite_inertia_is_never_filled(self):
        path = self.root/contract.URDF_NAME
        path.write_text(SOURCE.replace('<mass value="0.5"/>', '<mass value="nan"/>'))
        with self.assertRaises(PipelineError):
            mjcf.project(self.root)
        self.assertFalse((self.root/'mjcf').exists())

    def test_enabled_zero_range_is_preserved_as_an_explicit_support_limit(self):
        path = self.root/contract.URDF_NAME
        raw = SOURCE.replace('lower="-0.7" upper="1.2"', 'lower="0" upper="0"')
        path.write_text(raw)
        with self.assertRaisesRegex(PipelineError, 'cannot encode a zero-width range'):
            mjcf.project(self.root)
        self.assertEqual(raw, path.read_text())
        self.assertFalse((self.root/'mjcf').exists())

    def test_missing_moving_inertia_is_never_filled(self):
        tree = ET.fromstring(SOURCE)
        arm = tree.find('./link[@name="arm"]')
        arm.remove(arm.find('inertial'))
        (self.root/contract.URDF_NAME).write_bytes(ET.tostring(tree))
        with self.assertRaisesRegex(PipelineError, 'declares no <inertial>'):
            mjcf.project(self.root)
        self.assertFalse((self.root/'mjcf').exists())

    def boundary_child(self, report, suffix):
        payload = {**report, 'checks': [row for row in report['checks'] if row['id'] != 'identity.stability']}
        script = self.root/'boundary-child.py'
        script.write_text('import sys\nprint(' + repr(json.dumps(payload)) + ')\n' + suffix)
        return script

    def test_valid_json_cannot_hide_a_failed_verifier_process(self):
        report = self.build()
        self.assertTrue(report['passed'])
        binding = self.binding()
        script = self.boundary_child(report, 'sys.exit(3)\n')
        with patch('description_pipeline.verification.mjcf.__file__', str(script)):
            failed = evaluate_projection(self.root, binding)
        self.assertFalse(failed['passed'])
        self.assertIn('exited with status 3', failed['checks'][0]['details']['error'])

    def test_a_change_after_child_report_is_rejected(self):
        report = self.build()
        self.assertTrue(report['passed'])
        binding = self.binding()
        target = str(self.root/contract.ROBOT_NAME)
        script = self.boundary_child(report, 'with open(' + repr(target) + ', "ab") as stream: stream.write(b"\\n")\n')
        with patch('description_pipeline.verification.mjcf.__file__', str(script)):
            failed = evaluate_projection(self.root, binding)
        self.assertFalse(failed['passed'])
        self.assertIn('identity.stability', {row['id'] for row in failed['checks'] if not row['passed']})

    def test_sealed_configuration_preserves_sparse_user_bytes(self):
        raw = b'{\n  "base": "fixed", "simulation": {"timestep": 0.002}\n}\n'
        mjcf.project(self.root, raw)
        self.assertEqual(raw, (self.root/contract.CONFIG_NAME).read_bytes())
        report = evaluate_projection(self.root, self.binding())
        self.assertTrue(report['passed'], report['checks'])
        self.assertEqual(0.002, report['facts']['effective_simulation']['simulation']['timestep'])
        self.assertTrue(np.isfinite(report['facts']['effective_simulation']['simulation']['gravity']).all())


if __name__ == '__main__':
    unittest.main()
