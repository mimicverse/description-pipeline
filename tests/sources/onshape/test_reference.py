"""来源身份：URL/显式 ID → 站点 + 文档 + (工作区|版本) + 元素。"""

import json
import unittest

from description_pipeline.sources.onshape.errors import REFERENCE_INVALID, OnshapeSourceError
from description_pipeline.sources.onshape.reference import DocumentRef, parse_reference

from .helpers import DOCUMENT_ID, ROOT_ELEMENT, URL, WORKSPACE_ID


class ParseReferenceTests(unittest.TestCase):
    def test_url_resolves_stack_document_workspace_and_element(self):
        ref = parse_reference(URL)
        self.assertEqual(ref.stack, "https://cad.onshape.com")
        self.assertEqual(ref.document_id, DOCUMENT_ID)
        self.assertEqual(ref.workspace_id, WORKSPACE_ID)
        self.assertIsNone(ref.version_id)
        self.assertEqual(ref.element_id, ROOT_ELEMENT)
        self.assertEqual(ref.wvm, "w")
        self.assertEqual(ref.wvmid, WORKSPACE_ID)
        self.assertEqual(ref.url, URL)

    def test_version_url_locks_the_revision(self):
        ref = parse_reference(URL.replace("/w/", "/v/"))
        self.assertEqual(ref.version_id, WORKSPACE_ID)
        self.assertIsNone(ref.workspace_id)
        self.assertEqual(ref.wvm, "v")
        self.assertTrue(ref.lock(microversion=None, configuration="default")["revision_locked"])

    def test_explicit_ids_win_over_url(self):
        ref = parse_reference(URL, document_id="doc2", workspace_id="ws2", element_id="element2")
        self.assertEqual((ref.document_id, ref.element_id, ref.workspace_id), ("doc2", "element2", "ws2"))

    def test_other_stack_is_kept(self):
        ref = parse_reference(URL.replace("cad.onshape.com", "cad.example.com"))
        self.assertEqual(ref.stack, "https://cad.example.com")

    def test_missing_element_is_rejected(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_reference(f"https://cad.onshape.com/documents/{DOCUMENT_ID}/w/{WORKSPACE_ID}")
        self.assertEqual(caught.exception.code, REFERENCE_INVALID)
        self.assertEqual(caught.exception.detail["missing"], ["element_id"])

    def test_url_without_workspace_or_version_is_rejected(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_reference(f"https://cad.onshape.com/documents/{DOCUMENT_ID}/e/{ROOT_ELEMENT}")
        self.assertEqual(caught.exception.code, REFERENCE_INVALID)

    def test_url_without_host_is_rejected(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_reference(f"documents/{DOCUMENT_ID}/w/{WORKSPACE_ID}/e/{ROOT_ELEMENT}")
        self.assertEqual(caught.exception.code, REFERENCE_INVALID)

    def test_workspace_and_version_together_are_rejected(self):
        for workspace, version in ((WORKSPACE_ID, "ver1"), (None, None)):
            with self.subTest(workspace=workspace, version=version):
                with self.assertRaises(OnshapeSourceError) as caught:
                    DocumentRef(
                        stack="https://cad.onshape.com",
                        document_id=DOCUMENT_ID,
                        element_id=ROOT_ELEMENT,
                        workspace_id=workspace,
                        version_id=version,
                    )
                self.assertEqual(caught.exception.code, REFERENCE_INVALID)

    def test_identity_and_lock_carry_no_credentials(self):
        payload = parse_reference(URL).lock(microversion="mv-1", configuration="default")
        self.assertEqual(payload["microversion"], "mv-1")
        self.assertEqual(payload["configuration"], "default")
        self.assertEqual(payload["revision_kind"], "workspace")
        self.assertNotIn("key", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
