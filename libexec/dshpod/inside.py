#!/usr/bin/python3
"""Runs only inside the isolated container; all persistent writes go to HOME."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

HOME = Path.home()
DATA = HOME / ".local/share/dshpod"
TOOLS = DATA / "tools"
RELEASES = DATA / "releases"
CURRENT = DATA / "current"
BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


def environment():
    env = os.environ.copy()
    env["PATH"] = ":".join(map(str, (Path("/opt/dshpod"), TOOLS / "node/bin", TOOLS / "pnpm/bin",
                                      HOME / ".local/bin", HOME / ".local/share/pnpm"))) + ":" + BASE_PATH
    env["XDG_CACHE_HOME"] = str(HOME / ".cache")
    env["XDG_CONFIG_HOME"] = str(HOME / ".config")
    env["XDG_DATA_HOME"] = str(HOME / ".local/share")
    env["npm_config_cache"] = str(HOME / ".cache/npm")
    env["npm_config_prefix"] = str(HOME / ".local")
    env["npm_config_manage_package_manager_versions"] = "false"
    env["PNPM_HOME"] = str(HOME / ".local/share/pnpm")
    env["COREPACK_ENABLE_AUTO_PIN"] = "0"
    return env


def call(args, *, cwd=None, capture=False, env=None, check=True):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env or environment(),
                            text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode:
        raise RuntimeError((result.stderr or "").strip() or
                           f"command failed ({result.returncode}): {args[0]}")
    return result


def fetch(url, destination=None, *, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": "dshpod/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if destination is None:
            return response.read()
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise RuntimeError(f"unsupported version: {value}")
    return tuple(map(int, match.groups()))


def satisfies(version, specification):
    """The stable semver comparators used by Node/pnpm engine declarations.

    Reject unfamiliar grammar instead of silently installing an incompatible runtime.
    """
    current = version_tuple(version)
    for clause in specification.split("||"):
        clause = re.sub(r"([<>]=?|[=^~])\s+", r"\1", clause.strip())
        valid = True
        for token in clause.split():
            match = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", token)
            if not match:
                if token in ("*", "x"):
                    continue
                raise RuntimeError(f"unsupported engines grammar: {specification}")
            op, major, minor, patch = match.groups()
            target = (int(major), int(minor) if minor and minor.isdigit() else 0,
                      int(patch) if patch and patch.isdigit() else 0)
            if op == "^":
                upper = (target[0] + 1, 0, 0) if target[0] else (
                    (0, target[1] + 1, 0) if target[1] else (0, 0, target[2] + 1))
                ok = target <= current < upper
            elif op == "~":
                upper = (target[0], target[1] + 1, 0) if minor else (target[0] + 1, 0, 0)
                ok = target <= current < upper
            elif op:
                ok = {">=": current >= target, "<=": current <= target,
                      ">": current > target, "<": current < target, "=": current == target}[op]
            elif minor is None or minor in ("x", "*"):
                ok = current[0] == target[0]
            elif patch is None or patch in ("x", "*"):
                ok = current[:2] == target[:2]
            else:
                ok = current == target
            valid = valid and ok
        if valid:
            return True
    return False


def node_info(path):
    if not path or not Path(path).exists():
        return None
    result = call([path, "-p", "JSON.stringify({version:process.version,ts:process.features.typescript,"
                   "exec:process.execPath})"], capture=True, check=False)
    if result.returncode:
        return None
    try:
        info = json.loads(result.stdout)
    except ValueError:
        return None
    info["headers"] = str(Path(info["exec"]).resolve().parent.parent / "include/node/node_api.h")
    return info


def usable_node(info, engines):
    return bool(info and info["ts"] and Path(info["headers"]).is_file()
                and all(satisfies(info["version"], spec) for spec in engines))


def atomic_link(target, link):
    temp = link.parent / (link.name + ".next")
    temp.unlink(missing_ok=True)
    temp.symlink_to(os.path.relpath(target, link.parent))
    temp.replace(link)


def install_node(engines, preferred_major):
    candidates = [shutil.which("node", path=BASE_PATH), TOOLS / "node/bin/node"]
    for candidate in candidates:
        info = node_info(candidate)
        if usable_node(info, engines):
            # A newly compatible host runtime can replace a downloaded fallback.
            if candidate != TOOLS / "node/bin/node":
                (TOOLS / "node").unlink(missing_ok=True)
            print(f"Node.js: {info['version']} ({candidate})", flush=True)
            return
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    if arch is None:
        raise RuntimeError("automatic Node download supports Linux x64 and arm64")
    index = json.loads(fetch("https://nodejs.org/dist/index.json"))
    releases = [item for item in index if item.get("lts") and
                f"linux-{arch}" in item.get("files", []) and
                all(satisfies(item["version"], spec) for spec in engines)]
    releases.sort(key=lambda item: (version_tuple(item["version"])[0] == preferred_major,
                                     version_tuple(item["version"])), reverse=True)
    if not releases:
        raise RuntimeError(f"no official LTS Node release satisfies {engines}")
    version = releases[0]["version"]
    name = f"node-{version}-linux-{arch}"
    final = TOOLS / name
    if not usable_node(node_info(final / "bin/node"), engines):
        with tempfile.TemporaryDirectory(prefix="node-", dir=TOOLS) as temp:
            directory = Path(temp)
            filename = name + ".tar.xz"
            root_url = f"https://nodejs.org/dist/{version}/"
            sums = fetch(root_url + "SHASUMS256.txt").decode()
            expected = next((line.split()[0] for line in sums.splitlines()
                             if line.split()[-1].lstrip("*") == filename), None)
            if not expected:
                raise RuntimeError("official checksum is missing")
            archive = directory / filename
            print(f"Downloading official Node.js {version} ({arch})", flush=True)
            fetch(root_url + filename, archive)
            with archive.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise RuntimeError("Node.js SHA-256 mismatch")
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(directory, filter="data")
            if not usable_node(node_info(directory / name / "bin/node"), engines):
                raise RuntimeError("downloaded Node.js does not satisfy the required features")
            if final.exists():
                shutil.rmtree(final)
            (directory / name).rename(final)
    atomic_link(final, TOOLS / "node")


def install_pnpm(version):
    # Prefer a compatible host installation; this also avoids global host writes.
    for candidate in (shutil.which("pnpm", path=BASE_PATH), TOOLS / "pnpm/bin/pnpm"):
        if not candidate or not Path(candidate).exists():
            continue
        result = call([candidate, "--version"], capture=True, check=False)
        if result.returncode == 0 and result.stdout.strip() == version:
            if candidate != TOOLS / "pnpm/bin/pnpm":
                (TOOLS / "pnpm").unlink(missing_ok=True)
            print(f"pnpm: {version} ({candidate})", flush=True)
            return
    final = TOOLS / ("pnpm-" + version)
    env = environment()
    # This is pnpm-specific; modern npm warns if it is passed to npm itself.
    env.pop("npm_config_manage_package_manager_versions", None)
    call(["npm", "install", "--global", "--prefix", final, "--ignore-scripts",
          "--no-audit", "--no-fund", "pnpm@" + version], env=env)
    atomic_link(final, TOOLS / "pnpm")
    result = call(["pnpm", "--version"], capture=True)
    if result.stdout.strip() != version:
        raise RuntimeError("pnpm version verification failed")


def update(args):
    for directory in (DATA, TOOLS, RELEASES):
        directory.mkdir(parents=True, exist_ok=True)
    with (DATA / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tool_links = {}
        for name in ("node", "pnpm"):
            link = TOOLS / name
            if link.exists() and not link.is_symlink():
                raise RuntimeError(f"managed tool link is not a symlink: {link}")
            tool_links[name] = os.readlink(link) if link.is_symlink() else None
        # Clone directly into the final release path: pnpm/native builds can record
        # absolute paths. Only the current symlink is changed after successful build.
        release = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=RELEASES))
        try:
            call(["git", "clone", "--depth=1", "--single-branch", "--", args.repository, release])
            package = json.loads((release / "package.json").read_text())
            manager = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(?:\+.*)?", package["packageManager"])
            if not manager:
                raise RuntimeError("repository does not pin a supported pnpm version")
            version = manager[1]
            metadata = json.loads(fetch("https://registry.npmjs.org/pnpm/" + version))
            engines = [package["engines"]["node"], metadata.get("engines", {}).get("node", ">=0.0.0")]
            install_node(engines, args.node_major)
            install_pnpm(version)
            env = environment()
            env["DSH_GATE_CONCURRENCY"] = str(args.concurrency)
            call(["pnpm", "install", "--frozen-lockfile"], cwd=release, env=env)
            call(["pnpm", "run", "build"], cwd=release, env=env)
            call(["pnpm", "dsh", "--version"], cwd=release, env=env)
            commit = call(["git", "rev-parse", "HEAD"], cwd=release, capture=True).stdout.strip()
            record = {"commit": commit, "version": package["version"], "pnpm": version,
                      "node_engines": engines, "repository": args.repository}
            (release / ".dshpod-release.json").write_text(json.dumps(record, indent=2) + "\n")
            if CURRENT.is_symlink() and CURRENT.exists():
                atomic_link(CURRENT.resolve(), DATA / "previous")
            atomic_link(release, CURRENT)
            print(f"dsh installed: {package['version']} ({commit})", flush=True)
        except BaseException:
            # A failed update must retain the old runtime and package manager too.
            for name, target in tool_links.items():
                link = TOOLS / name
                link.unlink(missing_ok=True)
                if target is not None:
                    link.symlink_to(target)
            print(f"Update failed; the current dsh installation is unchanged. Source retained for diagnostics: {release}", file=sys.stderr)
            raise


def require_install():
    record = CURRENT / ".dshpod-release.json"
    if not record.is_file():
        raise RuntimeError("dsh is not installed; run bin/dshpod dshupdate [--home DIR]")
    return json.loads(record.read_text())


def assert_temporary_writable(directories=("/tmp", "/var/tmp")):
    for directory in directories:
        try:
            with tempfile.TemporaryDirectory(prefix="dshpod-probe-", dir=directory):
                pass
        except OSError as exc:
            raise RuntimeError(f"temporary directory is not writable: {directory}: {exc}") from exc


def check():
    errors = []
    print(f"inside: user={os.environ['USER']} uid={os.getuid()} gid={os.getgid()} home={HOME}", flush=True)
    # Test actual read-only enforcement without changing existing host files.
    for directory in (Path("/"), Path("/usr"), Path("/etc"), Path("/etc/alternatives"), Path("/etc/ssl/certs")):
        try:
            with tempfile.NamedTemporaryFile(prefix=".dshpod-probe-", dir=directory):
                pass
            errors.append(f"unexpected writable path: {directory}")
        except OSError as exc:
            if exc.errno != 30:  # EROFS: permission denial alone is insufficient.
                errors.append(f"read-only probe {directory}: {exc}")
    try:
        with tempfile.TemporaryFile(dir=HOME) as file:
            file.write(b"dshpod")
        print("home: writable; system mounts: read-only", flush=True)
    except OSError as exc:
        errors.append(f"home write: {exc}")
    try:
        assert_temporary_writable()
        print("/tmp /var/tmp: writable", flush=True)
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        socket.getaddrinfo("github.com", 443)
        fetch("https://nodejs.org/dist/index.json", timeout=15)
        print("DNS / HTTPS certificates: OK", flush=True)
    except OSError as exc:
        errors.append(f"DNS/HTTPS: {exc}")
    try:
        record = require_install()
        info = node_info(shutil.which("node", path=environment()["PATH"]))
        if not usable_node(info, record["node_engines"]):
            raise RuntimeError("Node.js version, TypeScript support, or headers are insufficient")
        env = environment()
        env["COREPACK_ENABLE_NETWORK"] = "0"
        manager = call(["pnpm", "--version"], capture=True, env=env).stdout.strip()
        if manager != record["pnpm"]:
            raise RuntimeError(f"pnpm mismatch: expected {record['pnpm']}, found {manager}")
        if not (CURRENT / "node_modules").is_dir():
            raise RuntimeError("dsh node_modules is missing")
        call(["pnpm", "dsh", "--version"], cwd=CURRENT, env=env)
        print(f"dsh: {record['version']} commit={record['commit']}", flush=True)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    for error in errors:
        print("FAIL: " + error, file=sys.stderr)
    return 1 if errors else 0


def serve(args):
    require_install()
    interfaces = json.loads(call(["ip", "-j", "-4", "addr"], capture=True).stdout)
    ips = [entry["local"] for interface in interfaces for entry in interface.get("addr_info", [])
           if not ipaddress.IPv4Address(entry["local"]).is_loopback]
    if not ips:
        raise RuntimeError("container has no non-loopback IPv4 address")
    command = ["pnpm", "dsh", "web", "--no-open", "--host", ips[0], "--port", "3080",
               "--public-url", args.public_url, "--trusted-host", args.trusted_host]
    os.chdir(CURRENT)
    os.execvpe(command[0], command, environment())


def main():
    if os.environ.get("DSHPOD_IN_CONTAINER") != "1" or not Path("/run/.containerenv").exists():
        raise RuntimeError("inside.py must run inside the dshpod container")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "shell"):
        commands.add_parser(name)
    execute = commands.add_parser("exec")
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    cli = commands.add_parser("cli")
    cli.add_argument("argv", nargs=argparse.REMAINDER)
    updater = commands.add_parser("update")
    updater.add_argument("--repository", required=True)
    updater.add_argument("--node-major", type=int, required=True)
    updater.add_argument("--concurrency", type=int, required=True)
    server = commands.add_parser("serve")
    server.add_argument("--public-url", required=True)
    server.add_argument("--trusted-host", required=True)
    args = parser.parse_args()
    if args.command == "update":
        assert_temporary_writable()
        update(args)
        return 0
    if args.command == "check":
        return check()
    if args.command == "serve":
        serve(args)
    if args.command == "cli":
        require_install()
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        command = ["pnpm", "dsh", *argv]
        os.chdir(CURRENT)
        os.execvpe(command[0], command, environment())
    command = ["/bin/bash", "--noprofile"] if args.command == "shell" else args.argv
    if not command:
        raise RuntimeError("exec requires a command")
    os.chdir(HOME)
    os.execvpe(command[0], command, environment())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"dshpod (inside): {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
