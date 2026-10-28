"""Verified operator TLS probes use the configured origin host and trust anchor."""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import UTC, datetime, timedelta
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "deploy" / "operator" / "tls_probe.py"
HEALTH_PATH = ROOT / "deploy" / "operator" / "health.sh"


def load_probe():
    spec = importlib.util.spec_from_file_location("operator_tls_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_certificate(root: Path, name: str, san: str) -> tuple[Path, Path]:
    certificate = root / f"{name}.crt"
    key = root / f"{name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-addext",
            f"subjectAltName={san}",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return certificate, key


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/auth/feishu/health":
            payload = json.dumps({"configured": True, "request_context": True}).encode()
            content_type = "application/json"
        else:
            payload = b"ok\n"
            content_type = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


class TlsProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.probe = load_probe()

    def serve(self, certificate: Path, key: Path):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_configured_origin_host_and_trust_anchor_are_verified(self) -> None:
        certificate, key = write_certificate(self.root, "operator.test", "DNS:operator.test")
        port = self.serve(certificate, key)
        status, payload = self.probe.probe(
            f"https://operator.test:{port}/operator-healthz",
            certificate,
        )
        self.assertEqual((status, payload), (200, b"ok\n"))
        self.assertEqual(
            self.probe.main(
                [
                    "--url",
                    f"https://operator.test:{port}/operator-healthz",
                    "--cafile",
                    str(certificate),
                    "--expect-status",
                    "200",
                ]
            ),
            0,
        )
        self.assertEqual(
            self.probe.main(
                [
                    "--url",
                    f"https://operator.test:{port}/auth/feishu/health",
                    "--cafile",
                    str(certificate),
                    "--mode",
                    "feishu-health",
                ]
            ),
            0,
        )

    def test_configured_ca_signed_leaf_is_an_explicit_trust_anchor(self) -> None:
        ca_file, ca_key_file = write_certificate(self.root, "issuer", "DNS:issuer.test")
        certificate, key_file = write_certificate(self.root, "operator", "DNS:operator.test")
        ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_file.read_bytes(), password=None)
        leaf = x509.load_pem_x509_certificate(certificate.read_bytes())
        signed = (
            x509.CertificateBuilder()
            .subject_name(leaf.subject)
            .issuer_name(ca.subject)
            .public_key(leaf.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("operator.test")]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        certificate.write_bytes(signed.public_bytes(serialization.Encoding.PEM))
        port = self.serve(certificate, key_file)
        self.assertEqual(
            self.probe.probe(f"https://operator.test:{port}/operator-healthz", certificate),
            (200, b"ok\n"),
        )
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.probe.probe(f"https://wrong.test:{port}/operator-healthz", certificate)

    def test_wrong_san_is_refused_even_when_the_certificate_is_trusted(self) -> None:
        certificate, key = write_certificate(self.root, "wrong.invalid", "DNS:wrong.invalid")
        port = self.serve(certificate, key)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.probe.main(
                [
                    "--url",
                    f"https://operator.test:{port}/operator-healthz",
                    "--cafile",
                    str(certificate),
                    "--expect-status",
                    "200",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", output.getvalue())

    def test_replaced_certificate_and_key_are_not_trusted(self) -> None:
        trusted, _ = write_certificate(self.root, "trusted", "DNS:operator.test")
        replacement, replacement_key = write_certificate(self.root, "replacement", "DNS:operator.test")
        port = self.serve(replacement, replacement_key)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.probe.probe(f"https://operator.test:{port}/operator-healthz", trusted)

    def test_expired_certificate_is_refused_even_when_explicitly_trusted(self) -> None:
        certificate, key_file = write_certificate(self.root, "operator.test", "DNS:operator.test")
        current = x509.load_pem_x509_certificate(certificate.read_bytes())
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        expired = (
            x509.CertificateBuilder()
            .subject_name(current.subject)
            .issuer_name(current.issuer)
            .public_key(current.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=2))
            .not_valid_after(datetime.now(UTC) - timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("operator.test")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        certificate.write_bytes(expired.public_bytes(serialization.Encoding.PEM))
        port = self.serve(certificate, key_file)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.probe.probe(f"https://operator.test:{port}/operator-healthz", certificate)

    def test_feishu_health_rejects_non_object_json_without_traceback(self) -> None:
        with (
            mock.patch.object(self.probe, "probe", return_value=(200, b"[]")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                self.probe.main(["--url", "https://operator.test/", "--cafile", "unused", "--mode", "feishu-health"]),
                1,
            )

    def test_health_uses_no_insecure_curl_or_python_context(self) -> None:
        source = HEALTH_PATH.read_text(encoding="utf-8")
        self.assertNotIn(" -k ", source)
        self.assertNotIn("CERT_NONE", source)
        self.assertIn('"$HERE/tls_probe.py"', source)
        self.assertIn('--cafile "$OPERATOR_TLS_CERT"', source)


if __name__ == "__main__":
    unittest.main()
