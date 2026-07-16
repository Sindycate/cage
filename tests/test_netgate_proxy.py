import importlib.util
import ipaddress
import socket
import sys
import threading
import unittest
import unicodedata
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "netgate_proxy", ROOT / "netgate-proxy.py"
)
netgate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = netgate
SPEC.loader.exec_module(netgate)


def address_result(address: str, port: int = 443):
    if ":" in address:
        return (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, port, 0, 0),
        )
    return (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        (address, port),
    )


class DestinationValidationTests(unittest.TestCase):
    def test_rejects_non_public_destination_classes(self):
        blocked = [
            "0.0.0.0",
            "10.0.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "::",
            "::1",
            "fec0::1",
            "fe80::1",
            "ff02::1",
        ]

        for address in blocked:
            with self.subTest(address=address), patch.object(
                netgate.socket,
                "getaddrinfo",
                return_value=[address_result(address)],
            ):
                with self.assertRaises(netgate.UnsafeDestinationError):
                    netgate.resolve_public_endpoints("example.com", 443)

    def test_rejects_mixed_public_and_private_dns_answer(self):
        results = [
            address_result("8.8.8.8"),
            address_result("127.0.0.1"),
        ]
        with patch.object(netgate.socket, "getaddrinfo", return_value=results):
            with self.assertRaises(netgate.UnsafeDestinationError):
                netgate.resolve_public_endpoints("example.com", 443)

    def test_accepts_public_ipv4_and_ipv6_destinations(self):
        results = [
            address_result("8.8.8.8"),
            address_result("2606:4700:4700::1111"),
        ]
        with patch.object(netgate.socket, "getaddrinfo", return_value=results):
            endpoints = netgate.resolve_public_endpoints("Example.COM.", 443)

        self.assertEqual(endpoints[0][3], ("8.8.8.8", 443))
        self.assertEqual(endpoints[1][3], ("2606:4700:4700::1111", 443, 0, 0))

    def test_connect_uses_pinned_numeric_result_without_second_dns_lookup(self):
        getaddrinfo = MagicMock(return_value=[address_result("8.8.8.8")])
        fake_socket = MagicMock()
        with patch.object(netgate.socket, "getaddrinfo", getaddrinfo):
            endpoints = netgate.resolve_public_endpoints("example.com", 443)
            with patch.object(netgate.socket, "socket", return_value=fake_socket):
                connected = netgate.connect_resolved_endpoints(endpoints)

        self.assertIs(connected, fake_socket)
        self.assertEqual(getaddrinfo.call_count, 1)
        fake_socket.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_connect_restricts_tunnel_ports_and_malformed_authorities(self):
        self.assertEqual(
            netgate._parse_connect_authority("example.com:443"),
            ("example.com", 443),
        )
        self.assertEqual(
            netgate._parse_connect_authority("example.com:8443"),
            ("example.com", 8443),
        )
        for authority in [
            "example.com:22",
            "example.com:65535",
            "user@example.com:443",
            "example.com:443/path",
            "example.com:443\nignored",
        ]:
            with self.subTest(authority=authority):
                with self.assertRaises(netgate.RequestRejected):
                    netgate._parse_connect_authority(authority)

    def test_denied_domain_is_not_resolved(self):
        client_socket, proxy_socket = socket.socketpair()
        try:
            client_socket.sendall(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\nConnection: close\r\n\r\n"
            )
            with patch.object(netgate, "gate_domain", return_value=False), patch.object(
                netgate.socket, "getaddrinfo"
            ) as getaddrinfo:
                netgate.NetgateHandler(
                    proxy_socket,
                    ("127.0.0.1", 12345),
                    SimpleNamespace(),
                )

            response = client_socket.recv(4096)
            self.assertTrue(response.startswith(b"HTTP/1.0 403"), response)
            getaddrinfo.assert_not_called()
        finally:
            client_socket.close()
            proxy_socket.close()


class RequestLimitTests(unittest.TestCase):
    def headers(self, *pairs):
        headers = Message()
        for key, value in pairs:
            headers[key] = value
        return headers

    def test_accepts_body_at_limit_and_rejects_larger_body(self):
        at_limit = self.headers(
            ("Content-Length", str(netgate.MAX_REQUEST_BODY))
        )
        too_large = self.headers(
            ("Content-Length", str(netgate.MAX_REQUEST_BODY + 1))
        )

        self.assertEqual(
            netgate.validate_request_body_headers(at_limit),
            netgate.MAX_REQUEST_BODY,
        )
        with self.assertRaises(netgate.RequestRejected) as raised:
            netgate.validate_request_body_headers(too_large)
        self.assertEqual(raised.exception.status, 413)

    def test_rejects_ambiguous_or_unbounded_body_framing(self):
        cases = [
            self.headers(("Transfer-Encoding", "chunked")),
            self.headers(("Content-Length", "1"), ("Content-Length", "2")),
            self.headers(("Content-Length", "-1")),
            self.headers(("Content-Length", "9" * 10000)),
        ]
        for headers in cases:
            with self.subTest(headers=list(headers.items())):
                with self.assertRaises(netgate.RequestRejected):
                    netgate.validate_request_body_headers(headers)

    def test_oversized_request_is_rejected_before_dns_or_prompt(self):
        client_socket, proxy_socket = socket.socketpair()
        try:
            request = (
                "POST http://example.com/upload HTTP/1.1\r\n"
                "Host: example.com\r\n"
                f"Content-Length: {netgate.MAX_REQUEST_BODY + 1}\r\n"
                "Connection: close\r\n\r\n"
            )
            client_socket.sendall(request.encode("ascii"))
            with patch.object(netgate.socket, "getaddrinfo") as getaddrinfo, patch.object(
                netgate, "prompt_user"
            ) as prompt_user:
                netgate.NetgateHandler(
                    proxy_socket,
                    ("127.0.0.1", 12345),
                    SimpleNamespace(),
                )

            response = client_socket.recv(4096)
            self.assertTrue(response.startswith(b"HTTP/1.0 413"), response)
            getaddrinfo.assert_not_called()
            prompt_user.assert_not_called()
        finally:
            client_socket.close()
            proxy_socket.close()


class ClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.docker_interfaces = [
            (
                ipaddress.IPv4Address("172.17.0.1"),
                ipaddress.IPv4Network("172.17.0.0/16"),
            )
        ]

    def test_allows_loopback_and_default_docker_bridge_clients(self):
        self.assertTrue(
            netgate.client_is_allowed("127.0.0.1", "127.0.0.1", [])
        )
        self.assertTrue(
            netgate.client_is_allowed(
                "172.17.0.2", "172.17.0.1", self.docker_interfaces
            )
        )

    def test_rejects_lan_and_wrong_interface_clients(self):
        self.assertFalse(
            netgate.client_is_allowed(
                "192.168.1.50", "192.168.1.10", self.docker_interfaces
            )
        )
        self.assertFalse(
            netgate.client_is_allowed(
                "192.168.1.50", "172.17.0.1", self.docker_interfaces
            )
        )
        self.assertFalse(
            netgate.client_is_allowed(
                "172.17.0.2", "192.168.1.10", self.docker_interfaces
            )
        )

    def test_rejects_excess_concurrency_without_starting_a_thread(self):
        server = object.__new__(netgate.ThreadingProxyServer)
        server._worker_slots = threading.BoundedSemaphore(1)
        server._worker_slots.acquire()
        server.shutdown_request = MagicMock()
        request = MagicMock()
        try:
            server.process_request(request, ("127.0.0.1", 12345))
        finally:
            server._worker_slots.release()

        request.sendall.assert_called_once()
        self.assertIn(b"503 Service Unavailable", request.sendall.call_args.args[0])
        server.shutdown_request.assert_called_once_with(request)


class ProxyAuthenticationTests(unittest.TestCase):
    def test_expected_basic_proxy_authorization_is_constant_checked(self):
        token = "a" * 64
        headers = Message()
        headers["Proxy-Authorization"] = netgate.expected_proxy_authorization(token)

        self.assertTrue(netgate.proxy_authorization_valid(headers, token))
        self.assertFalse(netgate.proxy_authorization_valid(headers, "b" * 64))

    def test_unauthenticated_request_is_rejected_before_gate_or_dns(self):
        client_socket, proxy_socket = socket.socketpair()
        try:
            client_socket.sendall(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\nConnection: close\r\n\r\n"
            )
            server = SimpleNamespace(auth_token="a" * 64)
            with patch.object(netgate, "gate_domain") as gate, patch.object(
                netgate.socket, "getaddrinfo"
            ) as getaddrinfo:
                netgate.NetgateHandler(
                    proxy_socket,
                    ("192.0.2.10", 12345),
                    server,
                )

            response = client_socket.recv(4096)
            self.assertTrue(response.startswith(b"HTTP/1.0 407"), response)
            gate.assert_not_called()
            getaddrinfo.assert_not_called()
        finally:
            client_socket.close()
            proxy_socket.close()


class PromptSafetyTests(unittest.TestCase):
    def test_osascript_keeps_values_out_of_source_and_sanitizes_controls(self):
        malicious_domain = 'evil.example" & do shell script "false\n\x1b[31m'
        malicious_container = 'container\r\x00name"'
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0, stdout="Allow (project)\n")

        with patch.dict(
            netgate.CONFIG, {"container_name": malicious_container}
        ), patch.object(netgate.subprocess, "run", side_effect=fake_run):
            result = netgate._prompt_osascript(malicious_domain)

        self.assertEqual(result, "project")
        script = captured["args"][2]
        self.assertNotIn(malicious_domain, script)
        self.assertNotIn(malicious_container, script)
        self.assertIn("system attribute", script)
        for key in [
            "CAGE_NETGATE_PROMPT_DOMAIN",
            "CAGE_NETGATE_PROMPT_CONTAINER",
        ]:
            self.assertFalse(
                any(
                    unicodedata.category(char).startswith("C")
                    for char in captured["env"][key]
                )
            )

    def test_osascript_requires_an_exact_known_button_result(self):
        fake_result = SimpleNamespace(
            returncode=0,
            stdout="Allow (always) plus untrusted output\n",
        )
        with patch.object(netgate.subprocess, "run", return_value=fake_result):
            self.assertEqual(netgate._prompt_osascript("example.com"), "deny")

    def test_terminal_prompt_values_are_bounded_and_control_free(self):
        sanitized = netgate.sanitize_prompt_value("safe\n\x1b[31m" + "x" * 500)
        self.assertLessEqual(len(sanitized), 253)
        self.assertFalse(
            any(
                unicodedata.category(char).startswith("C")
                for char in sanitized
            )
        )


if __name__ == "__main__":
    unittest.main()
