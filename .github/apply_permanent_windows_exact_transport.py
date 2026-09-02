from pathlib import Path

exact_path = Path("controller/combined_pack_gamerule_fleet_exact_runner.py")
text = exact_path.read_text(encoding="utf-8")
text = text.replace(
    "import os\nimport pathlib\nimport subprocess\nimport sys\nimport uuid\n",
    "import json\nimport os\nimport pathlib\nimport subprocess\nimport sys\nimport time\nimport uuid\n",
    1,
)
attr_anchor = "    lifecycle_registered: bool = False\n    lifecycle_request_path: pathlib.Path | None = None\n"
attr_replacement = '''    lifecycle_registered: bool = False
    lifecycle_request_path: pathlib.Path | None = None
    lifecycle_command_path: pathlib.Path | None = None

    def command(self, command: str) -> int:
        command_path = getattr(self, "lifecycle_command_path", None)
        if command_path is None:
            return super().command(command)
        if not self.is_alive():
            raise RuntimeError(f"Cannot send command to stopped server: {command}")
        if command_path.exists():
            raise RuntimeError(f"previous CI command request is still pending: {command_path}")
        start = len(self.snapshot())
        token = uuid.uuid4().hex
        pending = getattr(self, "_pending_file_commands", None)
        if pending is None:
            pending = {}
            self._pending_file_commands = pending
        pending[start] = token
        payload = {"token": token, "command": command}
        print(f"> {command} [file-trigger token={token}]", flush=True)
        command_path.parent.mkdir(parents=True, exist_ok=True)
        command_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        return start

    def wait_command_output(self, start_index: int, timeout: float = 8.0) -> list[str]:
        pending = getattr(self, "_pending_file_commands", {})
        token = pending.get(start_index)
        if token is None:
            return super().wait_command_output(start_index, timeout)
        completion = f"ci command dispatch completed; token={token}; dispatched=".casefold()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.snapshot()[start_index:]
            matched = next((line for line in lines if completion in line.casefold()), None)
            if matched is not None:
                pending.pop(start_index, None)
                if "dispatched=true" not in matched.casefold():
                    raise RuntimeError(f"Endstone rejected CI command transport request: {matched}")
                return lines
            if not self.is_alive():
                raise RuntimeError("BDS exited before CI command dispatch acknowledgement")
            time.sleep(0.05)
        raise TimeoutError(
            f"Timed out after {timeout:.0f}s waiting for CI command dispatch acknowledgement: {token}"
        )
'''
if "lifecycle_command_path" not in text:
    if attr_anchor not in text:
        raise SystemExit("lifecycle class attribute anchor not found")
    text = text.replace(attr_anchor, attr_replacement, 1)
old_registration = '''    if "file-control=" in registration.lower():
        raw_path = registration.split("file-control=", 1)[1].strip()
        request_path = pathlib.Path(raw_path)
        if not request_path.is_absolute():
            request_path = (self.root / request_path).resolve()
        self.server.lifecycle_request_path = request_path
        self.server.lifecycle_registered = True
        self.check(
            "windows-framework-lifecycle",
            "PASS",
            shutdown_control="file-trigger",
            request_path=str(request_path),
            compatibility_command="cishutdown",
        )
    else:
        self.server.lifecycle_request_path = None
        self.server.lifecycle_registered = True
        self.check("windows-interactive-lifecycle", "PASS", shutdown_control="cishutdown")
'''
new_registration = '''    if "file-control=" in registration.lower():
        raw_path = registration.split("file-control=", 1)[1].strip()
        request_path = pathlib.Path(raw_path)
        if not request_path.is_absolute():
            request_path = (self.root / request_path).resolve()
        command_path: pathlib.Path | None = None
        if "command-control=" in registration.lower():
            raw_command_path = registration.split("command-control=", 1)[1].split(";", 1)[0].strip()
            command_path = pathlib.Path(raw_command_path)
            if not command_path.is_absolute():
                command_path = (self.root / command_path).resolve()
        self.server.lifecycle_request_path = request_path
        self.server.lifecycle_command_path = command_path
        self.server.lifecycle_registered = True
        self.check(
            "windows-framework-lifecycle",
            "PASS",
            shutdown_control="file-trigger",
            command_control="file-trigger" if command_path is not None else "console-compat",
            request_path=str(request_path),
            command_path=str(command_path) if command_path is not None else None,
            compatibility_command="cishutdown",
        )
    else:
        self.server.lifecycle_request_path = None
        self.server.lifecycle_command_path = None
        self.server.lifecycle_registered = True
        self.check("windows-interactive-lifecycle", "PASS", shutdown_control="cishutdown")
'''
if 'command_control="file-trigger"' not in text:
    if old_registration not in text:
        raise SystemExit("Windows registration block not found")
    text = text.replace(old_registration, new_registration, 1)
exact_path.write_text(text, encoding="utf-8")

exact_test = Path("tests/test_combined_pack_exact_runner.py")
tests = exact_test.read_text(encoding="utf-8")
tests = tests.replace(
    '            "[CiLifecycleControl] CI lifecycle control enabled; cishutdown registered",\n',
    '            "[CiLifecycleControl] CI lifecycle control enabled; cishutdown registered; "\n'
    '            "command-control=plugins/ci/command.request; file-control=plugins/ci/shutdown.request",\n',
    1,
)
insert_anchor = "    def test_framework_shutdown_process_sends_cishutdown(self) -> None:\n"
transport_tests = '''    def test_framework_file_command_writes_tokenized_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = _WaitProcess(returncode=0)
            server = _framework_server(process)
            request = root / "command.request"
            server.lifecycle_command_path = request
            server.is_alive = lambda: True  # type: ignore[method-assign]
            server.snapshot = lambda: ["ready"]  # type: ignore[method-assign]
            start = server.command("spark tps")
            self.assertEqual(start, 1)
            payload = json.loads(request.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], "spark tps")
            self.assertTrue(payload["token"])
            self.assertEqual(server._pending_file_commands[start], payload["token"])

    def test_framework_file_command_rejects_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = _WaitProcess(returncode=0)
            server = _framework_server(process)
            request = root / "command.request"
            request.write_text("pending", encoding="utf-8")
            server.lifecycle_command_path = request
            server.is_alive = lambda: True  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "previous CI command request is still pending"):
                server.command("spark tps")

    def test_framework_file_command_wait_requires_positive_dispatch_ack(self) -> None:
        process = _WaitProcess(returncode=0)
        server = _framework_server(process)
        server._pending_file_commands = {0: "abc123"}
        server.snapshot = lambda: [  # type: ignore[method-assign]
            "CI command dispatch requested; token=abc123; command=spark tps",
            "CI command dispatch completed; token=abc123; dispatched=true",
        ]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        output = server.wait_command_output(0, 0.2)
        self.assertEqual(len(output), 2)
        self.assertNotIn(0, server._pending_file_commands)

    def test_framework_file_command_wait_fails_closed_on_rejected_dispatch(self) -> None:
        process = _WaitProcess(returncode=0)
        server = _framework_server(process)
        server._pending_file_commands = {0: "abc123"}
        server.snapshot = lambda: [  # type: ignore[method-assign]
            "CI command dispatch completed; token=abc123; dispatched=false"
        ]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "rejected CI command transport request"):
            server.wait_command_output(0, 0.2)

'''
if "test_framework_file_command_writes_tokenized_request" not in tests:
    if insert_anchor not in tests:
        raise SystemExit("exact transport test anchor not found")
    tests = tests.replace(insert_anchor, transport_tests + insert_anchor, 1)
old_assert = '''        lifecycle = [
            fields
            for name, status, fields in validator.checks
            if name == "windows-interactive-lifecycle" and status == "PASS"
        ]
        self.assertEqual(lifecycle, [{"shutdown_control": "cishutdown"}])
'''
new_assert = '''        lifecycle = [
            fields
            for name, status, fields in validator.checks
            if name == "windows-framework-lifecycle" and status == "PASS"
        ]
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(lifecycle[0]["shutdown_control"], "file-trigger")
        self.assertEqual(lifecycle[0]["command_control"], "file-trigger")
        self.assertTrue(str(lifecycle[0]["request_path"]).endswith("plugins/ci/shutdown.request"))
        self.assertTrue(str(lifecycle[0]["command_path"]).endswith("plugins/ci/command.request"))
'''
if old_assert in tests:
    tests = tests.replace(old_assert, new_assert, 1)
exact_test.write_text(tests, encoding="utf-8")

final_path = Path("controller/combined_windows_final_runner.py")
final_text = final_path.read_text(encoding="utf-8")
final_text = final_text.replace("import json\nimport time\nimport uuid\n", "", 1)
final_text = final_text.replace(
    "_ORIGINAL_WAIT_COMMAND_OUTPUT = exact._FrameworkShutdownServerProcess.wait_command_output\n\n",
    "",
    1,
)
command_start = "def _command_control_path(server: exact._FrameworkShutdownServerProcess) -> Path:\n"
world_start = "def _world_directories(server_dir: Path) -> dict[str, Path]:\n"
if command_start in final_text:
    start = final_text.index(command_start)
    end = final_text.index(world_start, start)
    final_text = final_text[:start] + final_text[end:]
if "_file_control_command" in final_text or "_wait_file_control_command_output" in final_text:
    raise SystemExit("duplicate final-runner command transport remains")
final_text = final_text.replace(
    "# behavior-pack state-oracle, and provenance adapters before we replace only\n# the Windows bootstrap and hosted command transports below.\n",
    "# behavior-pack state-oracle, restart-safe command transport, and provenance\n# adapters before we replace only the Windows bootstrap below.\n",
    1,
)
final_path.write_text(final_text, encoding="utf-8")

final_test = Path("tests/test_combined_windows_final_runner.py")
final_tests = final_test.read_text(encoding="utf-8")
final_tests = final_tests.replace("import json\n", "", 1)
helper_class = "class _FileCommandServer:\n"
final_class = "class CombinedWindowsFinalRunnerTest(unittest.TestCase):\n"
if helper_class in final_tests:
    start = final_tests.index(helper_class)
    end = final_tests.index(final_class, start)
    final_tests = final_tests[:start] + final_tests[end:]
transport_start = "    def test_file_control_command_writes_tokenized_request(self) -> None:\n"
bootstrap_start = "    def test_windows_bootstrap_reuses_exactly_one_bds_created_world(self) -> None:\n"
if transport_start in final_tests:
    start = final_tests.index(transport_start)
    end = final_tests.index(bootstrap_start, start)
    final_tests = final_tests[:start] + final_tests[end:]
if "_file_control_command" in final_tests or "_wait_file_control_command_output" in final_tests:
    raise SystemExit("duplicate final-runner command tests remain")
final_test.write_text(final_tests, encoding="utf-8")
