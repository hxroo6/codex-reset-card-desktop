"""Pure diagnostics checks: no network, credentials, files or card operations."""
import json
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch
from xml.etree.ElementTree import ParseError

from panel_logic import network_error_details, safe_error


class NetworkDiagnosticsTests(unittest.TestCase):
    def check_kind(self, error, kind, code=None):
        result = network_error_details(error)
        self.assertEqual(set(result), {"kind", "code", "message", "detail"})
        self.assertEqual(result["kind"], kind)
        self.assertEqual(result["code"], code)
        self.assertTrue(result["message"])
        self.assertLessEqual(len(result["detail"]), 240)
        return result

    def test_http_statuses_remain_http_even_when_wrapped(self):
        for code in (401, 403, 404, 429, 500, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://example.invalid/", code, "failure", {}, None)
                self.check_kind(error, "http", code)
                self.check_kind(urllib.error.URLError(error), "http", code)

    def test_go_http_status_text(self):
        for text, code in (("HTTP 401 Unauthorized", 401), ("HTTP/1.1 403 Forbidden", 403),
                           ("request failed: status code: 429", 429), ("HTTP status 503", 503)):
            with self.subTest(text=text):
                self.check_kind(RuntimeError(text), "http", code)

    def test_timeout_and_wrapped_timeout(self):
        for error in (TimeoutError("timed out"), socket.timeout("read timed out"),
                      urllib.error.URLError(socket.timeout("timed out")),
                      RuntimeError("Client.Timeout exceeded while awaiting headers"),
                      RuntimeError("context deadline exceeded")):
            with self.subTest(error=type(error).__name__):
                self.check_kind(error, "timeout")

    def test_tls_eof_and_wrapped_ssl(self):
        for error in (ssl.SSLEOFError(8, "EOF in violation of protocol"),
                      ssl.SSLCertVerificationError(1, "certificate verify failed"),
                      urllib.error.URLError(ssl.SSLError("handshake failed")),
                      RuntimeError('Get "https://example.invalid/": EOF')):
            with self.subTest(error=type(error).__name__):
                result = self.check_kind(error, "tls")
                self.assertNotIn("网站故障", result["message"])

    def test_dns_and_wrapped_dns(self):
        for error in (socket.gaierror(-2, "Name or service not known"),
                      urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                      RuntimeError("lookup example.invalid: no such host")):
            with self.subTest(error=type(error).__name__):
                self.check_kind(error, "dns")

    def test_connection_failures(self):
        for error in (ConnectionRefusedError("connection refused"),
                      ConnectionResetError("connection reset by peer"),
                      urllib.error.URLError("unavailable proxy"),
                      RuntimeError("dial tcp: connectex: connection refused")):
            with self.subTest(error=type(error).__name__):
                self.check_kind(error, "connection")

    def test_parse_failures(self):
        for error in (json.JSONDecodeError("invalid", "{", 1), ValueError("unexpected EOF in data"),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                      ParseError("invalid RSS")):
            with self.subTest(error=type(error).__name__):
                self.check_kind(error, "parse")

    def test_explicit_exception_cause_is_inspected(self):
        error = RuntimeError("request failed")
        error.__cause__ = ssl.SSLError("handshake failed")
        self.check_kind(error, "tls")

    def test_cyclic_cause_does_not_loop(self):
        error = RuntimeError("unexplained failure")
        error.__cause__ = error
        self.check_kind(error, "unknown")

    def test_unknown_does_not_invent_network_root_cause(self):
        self.check_kind(RuntimeError("unexplained failure"), "unknown")

    def test_error_details_are_bounded_and_redacted(self):
        value = ("HTTPS://test-user:test-pass@proxy.invalid:7890 Bearer test-bearer "
                 "access_token=test-access " + "x" * 700)
        result = self.check_kind(RuntimeError(value), "unknown")
        for secret in ("test-user", "test-pass", "test-bearer", "test-access"):
            self.assertNotIn(secret, result["detail"])
        self.assertLessEqual(len(safe_error(value)), 600)

    def test_url_userinfo_is_removed_for_multiple_schemes(self):
        for url in ("https://test-user:test-pass@example.invalid/",
                    "http://test-user:test-pass@proxy.invalid:7890/",
                    "socks5://test-user:test-pass@proxy.invalid:7890",
                    "https://test-user:pass@with-at@example.invalid/"):
            with self.subTest(scheme=url.split(":", 1)[0]):
                cleaned = safe_error(url)
                self.assertNotIn("test-user", cleaned)
                self.assertNotIn("test-pass", cleaned)
                self.assertNotIn("pass@with-at", cleaned)
                self.assertIn("[已隐藏]@", cleaned)

    def test_diagnostics_do_not_request_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected request")):
            self.check_kind(urllib.error.URLError("connection refused"), "connection")


if __name__ == "__main__":
    unittest.main(verbosity=2)
