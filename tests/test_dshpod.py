import importlib.machinery
import importlib.util
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import hashlib
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parent.parent


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "installation"
        shutil.copytree(PROJECT / "bin", self.base / "bin")
        shutil.copytree(PROJECT / "libexec", self.base / "libexec")
        shutil.copytree(PROJECT / "config", self.base / "config",
                        ignore=shutil.ignore_patterns(".dshpod.lock"))
        self.m = module("controller", self.base / "bin/dshpod")
        self.default_runtime_dir = self.m.runtime_dir
        user_runtime = Path(self.temp.name) / "user-runtime"
        user_runtime.mkdir(mode=0o700)
        self.m.runtime_dir = lambda: user_runtime / "dshpod-test"

    def test_username_uid_gid_are_dynamic(self):
        self.m.USER = types.SimpleNamespace(pw_name="foo")
        self.m.CHOME = "/home/foo"
        with patch.object(os, "getuid", return_value=1234), patch.object(os, "getgid", return_value=5678):
            self.m.build_root()
            content = (self.m.ROOT / "etc/passwd").read_text()
            self.assertIn("foo:x:1234:5678::/home/foo:/bin/bash", content)
            self.assertIn("foo:x:5678:", (self.m.ROOT / "etc/group").read_text())
            args = self.m.run_args(self.base / "workspace", self.m.config())
        self.assertIn("--user=1234:5678", args)
        self.assertIn("--userns=keep-id", args)
        self.assertIn("--env=HOME=/home/foo", args)
        writable = [a for a in args if "type=bind," in a and ",rw," in a]
        self.assertEqual(len(writable), 1)
        self.assertIn("dst=/home/foo,", writable[0])
        self.assertIn("--rootfs", args)
        self.assertIn("--read-only", args)
        self.assertIn("--network=pasta", args)
        self.assertNotIn("--network=bridge", args)
        self.assertNotIn("--network=host", args)

    def test_temporary_directories_are_writable_with_restrictive_umask(self):
        previous = os.umask(0o077)
        try:
            self.m.build_root()
        finally:
            os.umask(previous)
        for name in ("tmp", "var/tmp"):
            self.assertEqual((self.m.ROOT / name).stat().st_mode & 0o7777, 0o1777)
            (self.m.ROOT / name).chmod(0o755)
        self.m.ensure_root()
        args = self.m.run_args(self.base / "workspace", self.m.config())
        for name in ("tmp", "var/tmp"):
            self.assertEqual((self.m.ROOT / name).stat().st_mode & 0o7777, 0o1777)
            self.assertIn(f"--tmpfs=/{name}:rw,exec,nosuid,nodev,mode=1777,notmpcopyup", args)

    def test_managed_paths_cannot_be_exposed_as_home(self):
        for path in (self.base, self.base.parent, self.base / "storage",
                     self.base / "rootfs/x", self.base / "bin", self.base / "libexec",
                     self.base / "libexec/dshpod"):
            with self.subTest(path=path), self.assertRaises(self.m.Error):
                self.m.home_path(str(path))
        alias = self.base / "workspace"
        alias.symlink_to(self.base / "storage", target_is_directory=True)
        with self.assertRaises(self.m.Error):
            self.m.home_path(str(alias))
        with self.assertRaises(self.m.Error):
            self.m.home_path(None)

    def test_build_preserves_workspace_and_external_home(self):
        workspace = self.base / "workspace"
        external = Path(self.temp.name) / "external"
        for path in (workspace, external):
            path.mkdir()
            (path / "code.txt").write_text("valuable code")
        self.m.build_root()
        (self.m.ROOT / "old-file").touch()
        self.m.build_root()
        self.assertFalse((self.m.ROOT / "old-file").exists())
        for path in (workspace, external):
            self.assertEqual((path / "code.txt").read_text(), "valuable code")

    def test_relocation_discards_only_private_runtime_state(self):
        self.m.prepare_store()
        (self.m.STORE / "graph-v2/stale").touch()
        self.m.build_root()
        workspace = self.base / "workspace"
        workspace.mkdir()
        (workspace / "code").write_text("keep")
        marker = self.m.STORE / "location.json"
        value = json.loads(marker.read_text())
        value["base"] = "/old/location"
        marker.write_text(json.dumps(value))
        self.m.prepare_store()
        self.assertFalse((self.m.STORE / "graph-v2/stale").exists())
        self.assertTrue((self.m.ROOT / ".dshpod-rootfs").exists())
        self.assertEqual((workspace / "code").read_text(), "keep")

    def test_podman_uses_persistent_store_and_user_runtime(self):
        self.m.prepare_store()
        calls = []
        with patch.object(self.m, "run", side_effect=lambda args, **kwargs: calls.append((args, kwargs))):
            self.m.pod("info")
        args, kwargs = calls[0]
        self.assertEqual(args[args.index("--root") + 1], self.m.STORE / "graph-v2")
        self.assertEqual(args[args.index("--runroot") + 1], self.m.runtime_dir() / "run")
        self.assertEqual(args[args.index("--tmpdir") + 1], self.m.runtime_dir() / "tmp")
        self.assertEqual(kwargs["env"]["XDG_RUNTIME_DIR"], str(self.m.runtime_dir() / "xdg"))

    def test_default_runtime_is_under_apparmor_allowed_user_run_directory(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.base / "custom-runtime")}):
            path = self.default_runtime_dir()
        self.assertEqual(path.parent, Path(f"/run/user/{os.getuid()}"))
        self.assertTrue(path.name.startswith("dshpod-"))

    def test_rejects_runtime_symlink(self):
        self.m.runtime_dir().symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(self.m.Error, "symlink"):
            self.m.prepare_store()

    def test_exec_preserves_argument_boundaries(self):
        arguments = ["printf", "%s", "a b; $(touch should-not-exist)", "--home"]
        parsed = self.m.parser().parse_args(["exec", "--home", "/tmp/home", "--", *arguments])
        self.assertEqual(parsed.argv, ["--", *arguments])
        calls = []
        def fake_pod(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 17)
        with patch.object(self.m, "pod", side_effect=fake_pod):
            code = self.m.inside_exec({}, ["exec", *arguments])
        self.assertEqual(code, 17)
        self.assertEqual(list(calls[0][-len(arguments):]), arguments)

    def web_info(self, port=3081, running=True):
        return {
            "State": {"Running": running, "Status": "running" if running else "exited", "Pid": 123},
            "NetworkSettings": {"Ports": {"3080/tcp": [
                {"HostIp": "192.0.2.10", "HostPort": str(port)}]}},
            "Mounts": [{"Destination": self.m.CHOME, "Source": str(self.base / "workspace")}],
        }

    def installed_web_home(self):
        home = self.base / "workspace"
        record = home / ".local/share/dshpod/current/.dshpod-release.json"
        record.parent.mkdir(parents=True)
        record.write_text("{}")
        self.m.STORE.mkdir()
        return home

    def test_port_search_skips_busy_ports_on_selected_address(self):
        with patch.object(self.m.socket, "socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"),
                                      OSError(errno.EADDRINUSE, "busy"), None]
            self.assertEqual(self.m.available_port("192.0.2.10", 3080), 3082)
        self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                         [("192.0.2.10", 3080), ("192.0.2.10", 3081), ("192.0.2.10", 3082)])

    def test_port_search_does_not_skip_other_errors(self):
        for error in (errno.EACCES, errno.EADDRNOTAVAIL):
            with self.subTest(error=error), patch.object(self.m.socket, "socket") as socket_factory:
                probe = socket_factory.return_value.__enter__.return_value
                probe.bind.side_effect = OSError(error, "cannot bind")
                with self.assertRaisesRegex(self.m.Error, "cannot check Web port"):
                    self.m.available_port("192.0.2.10", 3080)
                self.assertEqual(probe.bind.call_count, 1)

    def test_port_search_accepts_start_plus_10(self):
        with patch.object(self.m.socket, "socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError(errno.EADDRINUSE, "busy")] * 10 + [None]
            self.assertEqual(self.m.available_port("192.0.2.10", 3080), 3090)
        self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                         [("192.0.2.10", port) for port in range(3080, 3091)])

    def test_port_search_does_not_try_start_plus_11(self):
        with patch.object(self.m.socket, "socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError(errno.EADDRINUSE, "busy")] * 11 + [None]
            with self.assertRaisesRegex(self.m.Error, "between 3080 and 3090"):
                self.m.available_port("192.0.2.10", 3080)
        self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                         [("192.0.2.10", port) for port in range(3080, 3091)])

    def test_port_search_stops_at_65535(self):
        with patch.object(self.m.socket, "socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
            with self.assertRaisesRegex(self.m.Error, "no available Web port"):
                self.m.available_port("192.0.2.10", 65530)
        self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                         [("192.0.2.10", port) for port in range(65530, 65536)])

    def test_start_uses_selected_port_for_mapping_and_public_urls(self):
        home = self.installed_web_home()
        cfg = self.m.config()
        original_config = (self.base / "config/dshpod.json").read_bytes()
        with patch.object(self.m, "lan_ip", return_value="192.0.2.10"), \
             patch.object(self.m, "available_port", return_value=3082) as select_port, \
             patch.object(self.m, "run_args", return_value=[]), \
             patch.object(self.m, "pod") as pod, \
             patch.object(self.m, "state", return_value=self.web_info(3082)), \
             patch.object(self.m, "wait_web", return_value=0) as wait_web:
            self.assertEqual(self.m.start(home, cfg, None), 0)
        select_port.assert_called_once_with("192.0.2.10", 3080)
        args = pod.call_args.args
        self.assertEqual(args[args.index("--publish") + 1], "192.0.2.10:3082:3080")
        self.assertEqual(args[args.index("--public-url") + 1], "http://192.0.2.10:3082/")
        self.assertEqual(args[args.index("--trusted-host") + 1], "192.0.2.10:3082")
        wait_web.assert_called_once_with("http://192.0.2.10:3082/", cfg)
        self.assertEqual(cfg["port"], 3080)
        self.assertEqual((self.base / "config/dshpod.json").read_bytes(), original_config)

    def test_start_keeps_existing_published_port(self):
        cfg = self.m.config()
        with patch.object(self.m, "available_port") as select_port, \
             patch.object(self.m, "pod") as pod, \
             patch.object(self.m, "wait_web", return_value=0) as wait_web:
            self.assertEqual(self.m.start(self.base / "workspace", cfg, self.web_info(3082)), 0)
        select_port.assert_not_called()
        pod.assert_not_called()
        wait_web.assert_called_once_with("http://192.0.2.10:3082/", cfg)

    def test_start_does_not_retry_when_port_is_taken_after_check(self):
        home = self.installed_web_home()
        with patch.object(self.m, "lan_ip", return_value="192.0.2.10"), \
             patch.object(self.m, "available_port", return_value=3081) as select_port, \
             patch.object(self.m, "run_args", return_value=[]), \
             patch.object(self.m, "pod", side_effect=self.m.Error("Address already in use")) as pod, \
             patch.object(self.m, "wait_web") as wait_web:
            with self.assertRaisesRegex(self.m.Error, "Address already in use"):
                self.m.start(home, self.m.config(), None)
        self.assertEqual(select_port.call_count, 1)
        self.assertEqual(pod.call_count, 1)
        wait_web.assert_not_called()
        self.assertFalse((self.m.STORE / "active.json").exists())

    def test_wait_web_rejects_wrong_log_url_before_http_probe(self):
        for origin in ("http://192.0.2.10:3080/", "http://192.0.2.11:3081/",
                       "https://192.0.2.10:3081/", "http://user@192.0.2.10:3081/",
                       "http://192.0.2.10:invalid/"):
            with self.subTest(origin=origin), \
                 patch.object(self.m, "state", return_value=self.web_info()), \
                 patch.object(self.m, "logs_url", return_value=origin + "?token=test-secret"), \
                 patch.object(self.m, "web_ready", return_value=True) as probe:
                with self.assertRaisesRegex(self.m.Error, "URL does not match") as error:
                    self.m.wait_web("http://192.0.2.10:3081/", self.m.config())
                self.assertNotIn("test-secret", str(error.exception))
                probe.assert_not_called()

    def test_wait_web_checks_actual_mapping_before_http_probe(self):
        with patch.object(self.m, "state", return_value=self.web_info(3080)), \
             patch.object(self.m, "logs_url", return_value="http://192.0.2.10:3081/?token=test"), \
             patch.object(self.m, "web_ready", return_value=True) as probe:
            with self.assertRaisesRegex(self.m.Error, "container Web port does not match"):
                self.m.wait_web("http://192.0.2.10:3081/", self.m.config())
        probe.assert_not_called()

    def test_wait_web_checks_running_state_before_http_probe(self):
        with patch.object(self.m, "state", return_value=self.web_info(running=False)), \
             patch.object(self.m, "logs_url", return_value="http://192.0.2.10:3081/?token=test"), \
             patch.object(self.m, "web_ready", return_value=True) as probe, \
             patch.object(self.m, "pod"):
            with self.assertRaisesRegex(self.m.Error, "dsh exited during startup"):
                self.m.wait_web("http://192.0.2.10:3081/", self.m.config())
        probe.assert_not_called()

    def test_wait_web_reports_matching_url_without_consuming_token(self):
        token_url = "http://192.0.2.10:3081/?token=test"
        with patch.object(self.m, "state", return_value=self.web_info()), \
             patch.object(self.m, "logs_url", return_value=token_url), \
             patch.object(self.m, "web_ready", return_value=True) as probe, \
             patch.object(self.m.sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(self.m.wait_web("http://192.0.2.10:3081/", self.m.config()), 0)
            self.assertEqual(output.getvalue(), "Web UI: " + token_url + "\n")
        probe.assert_called_once_with("http://192.0.2.10:3081/")

    def test_status_rejects_url_that_does_not_match_actual_mapping(self):
        with patch.object(self.m, "prerequisites"), \
             patch.object(self.m, "ensure_root"), \
             patch.object(self.m, "state", return_value=self.web_info(3080)), \
             patch.object(self.m, "logs_url", return_value="http://192.0.2.10:3081/?token=test"), \
             patch.object(self.m.sys, "argv", ["dshpod", "status"]), \
             patch.object(self.m.sys, "stdout", new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(self.m.Error, "URL does not match"):
                self.m.main()
            self.assertNotIn("Web UI:", output.getvalue())

    def test_logs_follows_all_output_without_blocking_stop(self):
        calls = []
        def fake_pod(*args, **kwargs):
            calls.append((args, kwargs))
            # A separate command must be able to take the management lock while
            # the logging subprocess is streaming indefinitely.
            with (self.base / "config/.dshpod.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return subprocess.CompletedProcess(args, 17)
        with patch.object(self.m, "prerequisites"), \
             patch.object(self.m, "state", return_value={"Id": "current-container-id"}), \
             patch.object(self.m, "pod", side_effect=fake_pod), \
             patch.object(self.m.sys, "argv", ["dshpod", "logs"]):
            self.assertEqual(self.m.main(), 17)
        self.assertEqual(calls, [(("logs", "--follow", "--tail=-1", "current-container-id"),
                                 {"check": False})])

    def test_logs_without_container_reports_missing_target(self):
        with patch.object(self.m, "prerequisites"), \
             patch.object(self.m, "state", return_value=None), \
             patch.object(self.m, "pod") as pod, \
             patch.object(self.m.sys, "argv", ["dshpod", "logs"]):
            with self.assertRaisesRegex(self.m.Error, "no container logs available"):
                self.m.main()
        pod.assert_not_called()

    def test_build_rejects_home_and_check_accepts_home(self):
        with self.assertRaises(SystemExit):
            self.m.parser().parse_args(["build", "--home", "/tmp/home"])
        args = self.m.parser().parse_args(["check", "--home", "/tmp/home"])
        self.assertEqual(args.home, "/tmp/home")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.m = module("inside", PROJECT / "libexec/dshpod/inside.py")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.m.DATA = Path(self.temp.name)
        self.m.TOOLS = self.m.DATA / "tools"
        self.m.RELEASES = self.m.DATA / "releases"
        self.m.CURRENT = self.m.DATA / "current"

    def test_engine_selection(self):
        self.assertTrue(self.m.satisfies("v22.22.1", "^22.19.0 || >=24.0.0"))
        self.assertTrue(self.m.satisfies("v24.1.0", "^22.19.0 || >=24.0.0"))
        self.assertFalse(self.m.satisfies("v23.0.0", "^22.19.0 || >=24.0.0"))
        self.assertFalse(self.m.satisfies("v22.18.0", "^22.19.0 || >=24.0.0"))
        self.assertFalse(self.m.usable_node({"ts": False}, []))

    def test_temporary_directory_probe(self):
        self.m.assert_temporary_writable([self.temp.name])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, "temporary directory is not writable"):
            self.m.assert_temporary_writable([str(Path(self.temp.name) / "missing")])

    def test_dsh_wrapper_forwards_cli_flags(self):
        with patch.dict(os.environ, {"DSHPOD_IN_CONTAINER": "1"}), \
             patch.object(self.m.Path, "exists", return_value=True), \
             patch.object(self.m, "require_install"), \
             patch.object(self.m.os, "chdir"), \
             patch.object(self.m.os, "execvpe", side_effect=RuntimeError("exec boundary")) as execute, \
             patch.object(self.m.sys, "argv", ["inside.py", "cli", "--", "--version"]):
            with self.assertRaisesRegex(RuntimeError, "exec boundary"):
                self.m.main()
        self.assertEqual(execute.call_args.args[:2], ("pnpm", ["pnpm", "dsh", "--version"]))

    def node_archive(self):
        source = self.m.DATA / "archive-source"
        root = source / "node-v24.1.0-linux-x64"
        (root / "bin").mkdir(parents=True)
        (root / "include/node").mkdir(parents=True)
        node = root / "bin/node"
        node.write_text('#!/usr/bin/python3\nimport json, pathlib\nprint(json.dumps({"version":"v24.1.0", "ts":"strip", "exec":str(pathlib.Path(__file__).resolve())}))\n')
        node.chmod(0o755)
        (root / "include/node/node_api.h").touch()
        archive = self.m.DATA / "node.tar.xz"
        with tarfile.open(archive, "w:xz") as tar:
            tar.add(root, arcname=root.name)
        return archive.read_bytes()

    def test_official_node_fallback_and_checksum_enforcement(self):
        archive = self.node_archive()
        self.m.TOOLS.mkdir()
        digest = hashlib.sha256(archive).hexdigest()
        checksum = ["0" * 64]
        def fake_fetch(url, destination=None):
            if url.endswith("index.json"):
                return b'[{"version":"v24.1.0","lts":"Test","files":["linux-x64"]}]'
            if url.endswith("SHASUMS256.txt"):
                return f"{checksum[0]}  node-v24.1.0-linux-x64.tar.xz\n".encode()
            destination.write_bytes(archive)
        with patch.object(self.m, "fetch", side_effect=fake_fetch), \
             patch.object(self.m.shutil, "which", return_value=None), \
             patch.object(self.m.platform, "machine", return_value="x86_64"):
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                self.m.install_node([">=24.0.0"], 24)
            self.assertFalse((self.m.TOOLS / "node").exists())
            checksum[0] = digest
            self.m.install_node([">=24.0.0"], 24)
        info = self.m.node_info(self.m.TOOLS / "node/bin/node")
        self.assertTrue(self.m.usable_node(info, [">=24.0.0"]))

    def test_failed_build_keeps_previous_installation(self):
        old = self.m.DATA / "old"
        old.mkdir()
        self.m.CURRENT.symlink_to("old")
        self.m.TOOLS.mkdir()
        (self.m.TOOLS / "node").symlink_to("old-node")
        (self.m.TOOLS / "pnpm").symlink_to("old-pnpm")
        def fake_call(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                release = Path(args[-1])
                (release / "package.json").write_text(json.dumps({
                    "version": "1.0.0", "packageManager": "pnpm@11.28.5",
                    "engines": {"node": ">=24.0.0"}}))
            if args[:3] == ["pnpm", "run", "build"]:
                raise RuntimeError("deliberate build failure")
            return subprocess.CompletedProcess(args, 0, "", "")
        args = types.SimpleNamespace(repository="https://example.test/repo.git", node_major=24, concurrency=2)
        def replace_node(*args):
            (self.m.TOOLS / "node").unlink()
            (self.m.TOOLS / "node").symlink_to("new-node")
        def replace_pnpm(*args):
            (self.m.TOOLS / "pnpm").unlink()
            (self.m.TOOLS / "pnpm").symlink_to("new-pnpm")
        with patch.object(self.m, "call", side_effect=fake_call), \
             patch.object(self.m, "fetch", return_value=b'{"engines":{"node":">=22.0.0"}}'), \
             patch.object(self.m, "install_node", side_effect=replace_node), \
             patch.object(self.m, "install_pnpm", side_effect=replace_pnpm):
            with self.assertRaisesRegex(RuntimeError, "deliberate build failure"):
                self.m.update(args)
        self.assertEqual(self.m.CURRENT.resolve(), old)
        self.assertEqual(os.readlink(self.m.TOOLS / "node"), "old-node")
        self.assertEqual(os.readlink(self.m.TOOLS / "pnpm"), "old-pnpm")


if __name__ == "__main__":
    unittest.main()
