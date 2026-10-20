#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Find CADCraft's command line tool, or fetch it, and run it.

CADCraft (https://github.com/storytold/cadcraft, MIT or Apache-2.0) is a native
program rather than a Python package, so pip cannot install it into a sandbox.
In order of preference, the 'cadcraft-cli' used is:

  1. the one $CADCRAFT_CLI names, for a machine that keeps its own;
  2. one already on the PATH -- a machine that installed CADCraft, or this
     package's image ('Dockerfile'), which PartCAD runs the scripts in when its
     'docker' sandbox is in use and the image can be pulled;
  3. the release this module pins, downloaded once into the package's cache
     directory -- 'request["cache_dir"]', which PartCAD hands every script of
     this package -- and checked against the SHA-256 below before anything is
     taken out of it.

The cache is keyed by platform as well as version because one copy of this
package is reached from more than one: a 'docker' sandbox on a Mac runs this
file on Linux, with the same directory mounted. Where that directory cannot be
written, the user's cache directory is used instead.

Runs inside the sandbox, so it uses nothing but the standard library.
"""

import contextlib
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

CLI = "cadcraft-cli"

# The release downloaded where none is installed. Raise it together with every
# checksum below, the version and the Linux checksums in the 'Dockerfile' (a
# test holds the two to each other), and CACHE_VERSION in every script: another
# CADCraft may convert the same drawing differently.
VERSION = "0.8.0"
RELEASE_URL = "https://github.com/storytold/cadcraft/releases/download/v{version}/{asset}"
CHECKSUMS_ASSET = "SHA256SUMS.txt"

ENV_EXECUTABLE = "CADCRAFT_CLI"
ENV_VERSION = "CADCRAFT_VERSION"

# The checksums of the pinned release's archives, from its 'SHA256SUMS.txt'. A
# version not listed here (CADCRAFT_VERSION) is checked against the
# 'SHA256SUMS.txt' published with it instead.
PINNED_SHA256 = {
    "cadcraft-0.8.0-linux-x86_64.tar.gz": "df3c2e668439f0410564b1fa9d21827bb663eb3d01703902c41f02b9c5276193",
    "cadcraft-0.8.0-linux-aarch64.tar.gz": "e7e7a805d683433733a77951326163506f203bd5dee515f465a5c575669d3f85",
    "cadcraft-0.8.0-linux-riscv64.tar.gz": "a4201026df74e853f59926f5920fa822998ce7fe117496f9ccb7c5ccaf9c0e5e",
    "cadcraft-0.8.0-freebsd-x86_64.tar.gz": "49154b97b5561af1522dc43e97b3f92a72d232a2e02257117306d3a59a34eb2e",
    "cadcraft-cli-0.8.0-macos-universal.zip": "0647286d06e2bf5da9fd1303f481966962ad224b57b4fa7adf2087a1e387e30b",
    "cadcraft-0.8.0-windows-x64-portable.zip": "939f970955653720558a90b0c788a356903fbe3b085e1bfb33fb67441957f92d",
    "cadcraft-0.8.0-windows-arm64-portable.zip": "8b0ef8149346a303cb6c2d98ae14d941bd22a4a3f5ed94df014db6d473e327cc",
    "cadcraft-0.8.0-windows-x86-portable.zip": "2b8e58394580b6646144a577903cbe83097c5e9f96ea32c69c1e6a6e0401bb4a",
}

# What 'platform.machine()' says, in the spelling CADCraft names its archives in.
ARCHITECTURES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "riscv64": "riscv64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
}

# Which architectures each operating system has a command line build for. macOS
# has one universal build and is not listed.
SUPPORTED = {
    "linux": ("x86_64", "aarch64", "riscv64"),
    "freebsd": ("x86_64",),
    "windows": ("x86_64", "aarch64", "x86"),
}
WINDOWS_ARCHITECTURES = {"x86_64": "x64", "aarch64": "arm64", "x86": "x86"}

# How long one conversion may take. CADCraft's own notes say a 10 MB DWG once
# took two minutes; a deadline keeps a pathological drawing from holding a
# render job forever.
DEFAULT_TIMEOUT = 600

# A download refreshes its lock this often, and a lock nobody has touched for
# STALE_LOCK seconds belonged to a process that died holding it.
LOCK_HEARTBEAT = 5
STALE_LOCK = 120
LOCK_TIMEOUT = 1800


class CadcraftError(Exception):
    """CADCraft could not be found, fetched or run."""


def platform_key(system=None, machine=None):
    """This machine as CADCraft's archives name it: 'linux-x86_64', 'macos-universal', ..."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "darwin":
        return "macos-universal"
    if system not in SUPPORTED:
        raise CadcraftError("CADCraft publishes no build for %s; install it and put %s on the PATH" % (system, CLI))
    arch = ARCHITECTURES.get(machine)
    if arch not in SUPPORTED[system]:
        raise CadcraftError(
            "CADCraft publishes no build for %s on %s; install it and put %s on the PATH" % (system, machine, CLI)
        )
    return "%s-%s" % (system, arch)


def asset_name(version, key):
    """The release archive that carries 'cadcraft-cli' for this platform."""
    if key == "macos-universal":
        return "cadcraft-cli-%s-macos-universal.zip" % version
    system, arch = key.split("-", 1)
    if system == "windows":
        return "cadcraft-%s-windows-%s-portable.zip" % (version, WINDOWS_ARCHITECTURES[arch])
    return "cadcraft-%s-%s-%s.tar.gz" % (version, system, arch)


def executable_name(key):
    return CLI + (".exe" if key.startswith("windows-") else "")


def default_cache_dir():
    """This package's own directory, for a PartCAD that hands scripts no 'cache_dir'."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def user_cache_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "partcad-cadcraft")


def _usable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def executable(cache_dir=None):
    """The path of a 'cadcraft-cli' to run, downloading the pinned release if there is none."""
    explicit = os.environ.get(ENV_EXECUTABLE)
    if explicit:
        if not _usable(explicit):
            raise CadcraftError("$%s names '%s', which is not an executable file" % (ENV_EXECUTABLE, explicit))
        return explicit

    found = shutil.which(CLI)
    if found:
        return found

    version = str(os.environ.get(ENV_VERSION) or VERSION).lstrip("v")
    key = platform_key()
    name = executable_name(key)
    roots = [cache_dir or default_cache_dir(), user_cache_dir()]

    for root in roots:
        candidate = os.path.join(root, "cadcraft", version, key, name)
        if _usable(candidate):
            return candidate

    unwritable = []
    for root in roots:
        directory = os.path.join(root, "cadcraft", version, key)
        try:
            os.makedirs(directory, exist_ok=True)
            tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-").close()
        except OSError as e:
            unwritable.append("%s (%s)" % (directory, e))
            continue
        return _install(directory, version, key, name)
    raise CadcraftError("nowhere to download CADCraft to: %s" % "; ".join(unwritable))


def _log(message):
    # Standard error: PartCAD keeps a script's standard output for its answer.
    sys.stderr.write("cadcraft: %s\n" % message)
    sys.stderr.flush()


@contextlib.contextmanager
def _lock(path):
    """Hold 'path' as a lock file across processes.

    PartCAD builds the parts of a package in parallel, so the first build on a
    machine can have several processes wanting the one download. One downloads;
    the others wait, then find what it installed.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > STALE_LOCK:
                    os.remove(path)
                    continue
            except OSError:
                continue  # released between the two calls
            if time.monotonic() > deadline:
                raise CadcraftError("timed out waiting for another process to download CADCraft (%s)" % path)
            time.sleep(0.5)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        last = [time.monotonic()]

        def heartbeat():
            now = time.monotonic()
            if now - last[0] >= LOCK_HEARTBEAT:
                last[0] = now
                with contextlib.suppress(OSError):
                    os.utime(path)

        yield heartbeat
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def _ssl_context():
    # The system's roots, plus certifi's where it is installed: a python.org or
    # conda interpreter on macOS often has no usable system store, while a
    # machine behind an intercepting proxy has its CA only in the system one.
    context = ssl.create_default_context()
    with contextlib.suppress(Exception):
        import certifi

        context.load_verify_locations(certifi.where())
    return context


def _open(url):
    request = urllib.request.Request(url, headers={"User-Agent": "partcad-cadcraft"})
    try:
        return urllib.request.urlopen(request, context=_ssl_context(), timeout=60)
    except Exception as e:
        raise CadcraftError("could not download %s: %s" % (url, e)) from e


def _download(url, destination, heartbeat):
    """Write 'url' to 'destination' and return the SHA-256 of what was written."""
    digest = hashlib.sha256()
    with _open(url) as response, open(destination, "wb") as f:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            heartbeat()
    return digest.hexdigest()


def expected_sha256(version, asset):
    """The checksum 'asset' has to have, from the table above or from its own release."""
    if asset in PINNED_SHA256:
        return PINNED_SHA256[asset]
    url = RELEASE_URL.format(version=version, asset=CHECKSUMS_ASSET)
    with _open(url) as response:
        listing = response.read().decode("utf-8", "replace")
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == asset:
            return fields[0].lower()
    raise CadcraftError("%s does not list %s" % (url, asset))


def _member(names, wanted):
    """The archive member that is the executable: the shortest path with that file name."""
    matches = [name for name in names if name.replace("\\", "/").rsplit("/", 1)[-1] == wanted]
    if not matches:
        raise CadcraftError("the archive holds no %s" % wanted)
    return min(matches, key=len)


def _extract(archive, asset, name, target):
    partial = "%s.partial-%d" % (target, os.getpid())
    try:
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                member = _member([i.filename for i in z.infolist() if not i.is_dir()], name)
                with z.open(member) as source, open(partial, "wb") as destination:
                    shutil.copyfileobj(source, destination)
        else:
            with tarfile.open(archive, "r:gz") as t:
                member = _member([m.name for m in t.getmembers() if m.isfile()], name)
                source = t.extractfile(member)
                with source, open(partial, "wb") as destination:
                    shutil.copyfileobj(source, destination)
        # A zip carries no mode that Python restores, so it is set, not copied.
        os.chmod(partial, 0o755)
        os.replace(partial, target)
    finally:
        with contextlib.suppress(OSError):
            os.remove(partial)


def _install(directory, version, key, name):
    target = os.path.join(directory, name)
    with _lock(os.path.join(directory, ".lock")) as heartbeat:
        if _usable(target):
            return target  # another process installed it while this one waited

        asset = asset_name(version, key)
        expected = expected_sha256(version, asset)
        url = RELEASE_URL.format(version=version, asset=asset)
        _log("downloading %s into %s" % (url, directory))
        fd, archive = tempfile.mkstemp(dir=directory, prefix=".download-", suffix=os.path.splitext(asset)[1])
        os.close(fd)
        try:
            actual = _download(url, archive, heartbeat)
            if actual != expected:
                raise CadcraftError(
                    "%s has SHA-256 %s, not the published %s; refusing to run it" % (asset, actual, expected)
                )
            _extract(archive, asset, name, target)
        finally:
            with contextlib.suppress(OSError):
                os.remove(archive)
    _log("installed %s" % target)
    return target


def run(arguments, cache_dir=None, timeout=DEFAULT_TIMEOUT):
    """Run 'cadcraft-cli' with 'arguments' and return the completed process.

    Raises CadcraftError, with what the tool said, when it exits non-zero.
    """
    command = [executable(cache_dir)] + [str(a) for a in arguments]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise CadcraftError("cadcraft-cli %s took longer than %ss" % (" ".join(command[1:]), timeout)) from e
    if completed.returncode != 0:
        said = (completed.stderr or completed.stdout or "").strip()
        raise CadcraftError(
            "cadcraft-cli %s failed (exit %d): %s" % (" ".join(command[1:]), completed.returncode, said)
        )
    return completed


def _warnings(completed):
    """What the tool said beyond having done what it was asked ("wrote <file> (<n> bytes)")."""
    said = (completed.stderr or "") + "\n" + (completed.stdout or "")
    return [
        line.strip()
        for line in said.splitlines()
        if line.strip() and not line.startswith("wrote ") and not line.lstrip().startswith("{")
    ]


def convert(source, target, cache_dir=None, timeout=DEFAULT_TIMEOUT):
    """Convert 'source' to 'target', each in the format its extension names.

    Returns what CADCraft warned about on the way, as lines: the entities a
    format cannot hold are reported rather than dropped in silence.
    """
    completed = run(["convert", source, target], cache_dir=cache_dir, timeout=timeout)
    if not os.path.isfile(target):
        raise CadcraftError("cadcraft-cli reported success but wrote no %s" % target)
    return _warnings(completed)


def save_as(source, target, version=None, binary=False, cache_dir=None, timeout=DEFAULT_TIMEOUT):
    """Open 'source' and save it as 'target', a DWG or a DXF of the given version.

    'version' is one of 2000, 2004, 2007, 2010, 2013, 2018 (CADCraft's default,
    2018, when None); 'binary' writes a binary DXF, and is for DXF only.
    """
    params = {"path": os.path.abspath(target)}
    if version:
        params["version"] = str(version)
    if binary:
        params["binary"] = True
    completed = run(
        ["run", os.path.abspath(source), "--cmd", "saveas " + json.dumps(params)],
        cache_dir=cache_dir,
        timeout=timeout,
    )
    if not os.path.isfile(target):
        raise CadcraftError("cadcraft-cli reported success but wrote no %s" % target)
    return _warnings(completed)
