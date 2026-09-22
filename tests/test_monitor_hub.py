import io
import json
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener as urllib_build_opener
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import constants as constants_api, hub as hub_api
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorHubTests(MonitorTestCase):
    def test_http_hub_is_private_only(self):
        self.assertEqual(
            monitor.normalize_hub_url("http://127.0.0.1:17321/"),
            "http://127.0.0.1:17321",
        )
        self.assertEqual(
            monitor.normalize_hub_url("http://10.0.0.5:17321"),
            "http://10.0.0.5:17321",
        )
        for value in (
            "http://monitor.example.test",
            "http://localhost",
            "http://monitor.local",
            "http://0.0.0.0:17321",
            "https://user:pass@monitor.example.test",
            "https://monitor.example.test/?secret=1",
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.normalize_hub_url(value)

    def test_hub_request_uses_auth_and_a_real_redirect_handler(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b'{"ok":true}'

        seen = {}

        class Opener:
            def open(self, request, timeout):
                seen["request"] = request
                seen["timeout"] = timeout
                return Response()

        connection = monitor.MonitorConnection(
            "https://monitor.example.test", "hub-secret"
        )
        with patch('cage_core.monitoring.hub.build_opener', return_value=Opener()) as build:
            self.assertEqual(
                hub_api._hub_request(connection, "GET", "/api/stats"),
                {"ok": True},
            )
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, HTTPRedirectHandler)
        self.assertEqual(seen["request"].headers["Authorization"], "Bearer hub-secret")
        self.assertEqual(seen["timeout"], 30)
        urllib_build_opener(hub_api._NoRedirect())
        with self.assertRaisesRegex(monitor.MonitorError, "redirect refused"):
            hub_api._NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://other.example"
            )

    def test_hub_multi_device_responses_have_a_separate_bounded_budget(self):
        connection = monitor.MonitorConnection("https://monitor.example.test", "secret")
        # Both endpoints return the complete multi-device stats, even when
        # the uploaded single-device payload is much smaller than 1 MiB.
        raw = json.dumps({"ok": True, "stats": {"padding": "x" * constants_api.MAX_OUTPUT_BYTES}}).encode()
        for method, path in (("GET", "/api/stats"), ("POST", "/api/ingest")):
            with self.subTest(method=method):
                with patch.object(hub_api, "build_opener") as build:
                    build.return_value.open.return_value = io.BytesIO(raw)
                    result = hub_api._hub_request(connection, method, path)
                self.assertTrue(result["ok"])

        # A small patched cap proves that reads stop at the bound, rather
        # than trusting Content-Length or buffering an unlimited response.
        with patch.object(constants_api, "MAX_HUB_RESPONSE_BYTES", 32):
            with patch.object(hub_api, "build_opener") as build:
                response = io.BytesIO(b" " * 100)
                build.return_value.open.return_value = response
                with patch.object(response, "read", wraps=response.read) as read:
                    with self.assertRaisesRegex(monitor.MonitorError, "response is too large"):
                        hub_api._hub_request(connection, "GET", "/api/stats")
                    read.assert_called_once_with(33)

    def test_hub_response_budget_does_not_relax_upload_size_or_json_validation(self):
        connection = monitor.MonitorConnection("https://monitor.example.test", "secret")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(hub_api, "_outbound_payload", return_value={"padding": "x" * constants_api.MAX_OUTPUT_BYTES}):
                with patch.object(hub_api, "_hub_request") as request:
                    with self.assertRaisesRegex(monitor.MonitorError, "ingest payload is too large"):
                        hub_api.upload_summary(connection, {}, config_root=Path(temporary))
                    request.assert_not_called()
        with patch.object(hub_api, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b"not json")
            with self.assertRaisesRegex(monitor.MonitorError, "invalid JSON"):
                hub_api._hub_request(connection, "POST", "/api/ingest")

    def test_hub_http_errors_do_not_persist_response_body(self):
        secret = "reflected-hub-secret"
        error = HTTPError(
            "https://monitor.example.test/api/stats",
            500,
            "server error",
            {},
            io.BytesIO(secret.encode()),
        )

        class Opener:
            def open(self, _request, timeout):
                raise error

        with patch('cage_core.monitoring.hub.build_opener', return_value=Opener()):
            with self.assertRaises(monitor.MonitorError) as raised:
                hub_api._hub_request(
                    monitor.MonitorConnection("https://monitor.example.test", secret),
                    "GET",
                    "/api/stats",
                )
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(str(raised.exception), "Token Monitor hub returned HTTP 500")

    def test_connection_verification_requires_real_hub_shapes(self):
        connection = monitor.MonitorConnection(
            "https://monitor.example.test", "hub-secret"
        )
        with patch(
            'cage_core.monitoring.hub._hub_request',
            side_effect=[{"ok": True, "role": "hub"}, {"devices": [], "periods": {}}],
        ) as request:
            monitor.verify_connection(connection)
        self.assertEqual(request.call_args_list[0].args[0].secret, "unused")
        self.assertEqual(request.call_args_list[1].args[0].secret, "hub-secret")

        with patch(
            'cage_core.monitoring.hub._hub_request',
            side_effect=[{"ok": True, "role": "hub"}, {"unexpected": True}],
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "authentication check failed"):
                monitor.verify_connection(connection)

    def test_upload_pseudonymizes_session_ids_at_the_hub_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device_id = monitor.host_device_id(root)
            record = monitor.VolumeRegistration(
                "a" * 32,
                device_id,
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            raw_session_id = "source-session-123"
            payload, _status = monitor.aggregate_summaries(
                root,
                [
                    (
                        record,
                        self._summary(
                            device_id,
                            {
                                f"codex:{raw_session_id}": self._session(
                                    raw_session_id,
                                    total=100,
                                    input_tokens=80,
                                    output_tokens=20,
                                )
                            },
                        ),
                    )
                ],
            )
            calls = []

            def hub_request(_connection, method, path, body=None):
                calls.append((method, path, body))
                return {}

            connection = monitor.MonitorConnection("https://hub.example", "secret")
            with patch.object(hub_api, "_hub_request", side_effect=hub_request):
                hub_api.upload_summary(connection, payload, config_root=root)
                hub_api.upload_summary(connection, payload, config_root=root)

            self.assertEqual(
                payload["today"]["sessions"][f"codex:{raw_session_id}"]["sessionId"],
                raw_session_id,
            )
            first = json.loads(calls[0][2])
            second = json.loads(calls[1][2])
            rendered = json.dumps(first, sort_keys=True)
            self.assertNotIn(raw_session_id, rendered)
            pseudonym = hub_api._outbound_session_id(root, "codex", raw_session_id)
            self.assertEqual(
                first["today"]["sessions"][f"codex:{pseudonym}"]["sessionId"],
                pseudonym,
            )
            self.assertEqual(first["today"]["sessions"], second["today"]["sessions"])
