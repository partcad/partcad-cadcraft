#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Finding and fetching 'cadcraft-cli', against a fake release served from a directory.

Standard library only, like the module: these run anywhere. The one test that
asks GitHub is opt-in ('CADCRAFT_NETWORK_TESTS=1').
"""

import hashlib
import io
import os
import tarfile
import threading
import time
import types
import urllib.request
import zipfile

import pytest
from conftest import plugin_module

FAKE_VERSION = "9.9.9"

FAKE_CLI = b"""#!/bin/sh
case "$1" in
  --version) echo "cadcraft-cli 9.9.9" ;;
  convert) cp "$2" "$3"; echo "wrote $3 (1 bytes)" >&2; echo "note: 1 entity dropped" >&2 ;;
  run) echo '{"path": "x"}' ;;
  fail) echo "boom" >&2; exit 3 ;;
esac
"""

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake cadcraft-cli is a shell script")


@pytest.fixture
def tool():
    return plugin_module("cadcraft_tool")


def write_archive(path, asset, name, content):
    top = asset[: -len(".tar.gz")] if asset.endswith(".tar.gz") else asset[: -len(".zip")]
    if asset.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("%s/README.md" % top, "docs")
            z.writestr("%s/%s" % (top, name), content)
    else:
        with tarfile.open(path, "w:gz") as t:
            for member, data, mode in (("%s/bin/%s" % (top, name), content, 0o755), ("%s/share/doc" % top, b"", 0o644)):
                info = tarfile.TarInfo(member)
                info.size, info.mode = len(data), mode
                t.addfile(info, io.BytesIO(data))


@pytest.fixture
def release(tool, tmp_path, monkeypatch):
    """A release of FAKE_VERSION for this platform, nothing on the PATH, and two empty cache roots."""
    key = tool.platform_key()
    asset = tool.asset_name(FAKE_VERSION, key)
    name = tool.executable_name(key)
    directory = tmp_path / "release" / ("v" + FAKE_VERSION)
    directory.mkdir(parents=True)
    write_archive(directory / asset, asset, name, FAKE_CLI)
    digest = hashlib.sha256((directory / asset).read_bytes()).hexdigest()
    (directory / tool.CHECKSUMS_ASSET).write_text("%s  %s\n" % (digest, asset))

    cache, user = tmp_path / "plugin" / ".cache", tmp_path / "user"
    monkeypatch.setattr(tool, "RELEASE_URL", (tmp_path / "release").as_uri() + "/v{version}/{asset}")
    monkeypatch.setattr(tool, "user_cache_dir", lambda: str(user))
    monkeypatch.setattr(tool.shutil, "which", lambda _name: None)
    monkeypatch.delenv(tool.ENV_EXECUTABLE, raising=False)
    monkeypatch.setenv(tool.ENV_VERSION, FAKE_VERSION)

    downloads = []
    real_download = tool._download

    def counting_download(url, destination, heartbeat):
        downloads.append(url)
        return real_download(url, destination, heartbeat)

    monkeypatch.setattr(tool, "_download", counting_download)
    return types.SimpleNamespace(
        directory=directory, asset=asset, key=key, name=name, cache=cache, user=user, downloads=downloads
    )


@pytest.mark.parametrize(
    "system, machine, key",
    [
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "riscv64", "linux-riscv64"),
        ("Darwin", "arm64", "macos-universal"),
        ("Darwin", "x86_64", "macos-universal"),
        ("Windows", "AMD64", "windows-x86_64"),
        ("Windows", "ARM64", "windows-aarch64"),
        ("FreeBSD", "amd64", "freebsd-x86_64"),
    ],
)
def test_every_platform_cadcraft_builds_for_has_a_key(tool, system, machine, key):
    assert tool.platform_key(system, machine) == key


@pytest.mark.parametrize("system, machine", [("Linux", "mips"), ("FreeBSD", "arm64"), ("SunOS", "sparc")])
def test_a_machine_with_no_release_is_told_what_to_do(tool, system, machine):
    with pytest.raises(tool.CadcraftError, match="PATH"):
        tool.platform_key(system, machine)


def test_the_pinned_checksums_cover_every_platform_and_nothing_else(tool):
    keys = ["macos-universal"] + ["%s-%s" % (s, a) for s, arches in tool.SUPPORTED.items() for a in arches]
    assert {tool.asset_name(tool.VERSION, key) for key in keys} == set(tool.PINNED_SHA256)


def test_one_on_the_path_is_used_as_it_is(tool, monkeypatch, tmp_path):
    monkeypatch.delenv(tool.ENV_EXECUTABLE, raising=False)
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/opt/cadcraft/bin/" + name)
    assert tool.executable(str(tmp_path)) == "/opt/cadcraft/bin/cadcraft-cli"
    assert list(tmp_path.iterdir()) == []


def test_the_environment_names_one_to_use_before_the_path(tool, monkeypatch, tmp_path):
    mine = tmp_path / "my-cadcraft-cli"
    mine.write_bytes(FAKE_CLI)
    mine.chmod(0o755)
    monkeypatch.setenv(tool.ENV_EXECUTABLE, str(mine))
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/opt/cadcraft/bin/" + name)
    assert tool.executable() == str(mine)

    monkeypatch.setenv(tool.ENV_EXECUTABLE, str(tmp_path / "missing"))
    with pytest.raises(tool.CadcraftError, match="not an executable"):
        tool.executable()


def test_a_release_is_downloaded_once_into_the_cache_directory(tool, release):
    path = tool.executable(str(release.cache))

    assert path == str(release.cache / "cadcraft" / FAKE_VERSION / release.key / release.name)
    assert os.access(path, os.X_OK)
    assert open(path, "rb").read() == FAKE_CLI
    # Nothing is left of the download but the tool: no archive, no lock.
    assert os.listdir(os.path.dirname(path)) == [release.name]

    # A second part finds it there, with the release gone.
    for item in release.directory.iterdir():
        item.unlink()
    assert tool.executable(str(release.cache)) == path
    assert len(release.downloads) == 1


def test_without_a_cache_directory_the_package_s_own_is_used(tool):
    assert tool.default_cache_dir() == os.path.join(os.path.dirname(os.path.abspath(tool.__file__)), ".cache")


def test_a_release_that_does_not_match_its_checksum_is_refused(tool, release):
    (release.directory / tool.CHECKSUMS_ASSET).write_text("%s  %s\n" % ("0" * 64, release.asset))

    with pytest.raises(tool.CadcraftError, match="refusing to run it"):
        tool.executable(str(release.cache))
    assert os.listdir(release.cache / "cadcraft" / FAKE_VERSION / release.key) == []


def test_the_pinned_table_is_what_a_pinned_asset_is_checked_against(tool, release, monkeypatch):
    monkeypatch.setitem(tool.PINNED_SHA256, release.asset, "f" * 64)
    with pytest.raises(tool.CadcraftError, match="not the published " + "f" * 64):
        tool.executable(str(release.cache))


def test_an_unwritable_cache_directory_falls_back_to_the_user_s(tool, release):
    # A file where the directory would go is unwritable for root too.
    release.cache.parent.mkdir(parents=True)
    release.cache.write_text("not a directory")
    assert tool.executable(str(release.cache)) == str(
        release.user / "cadcraft" / FAKE_VERSION / release.key / release.name
    )


def test_parts_built_at_once_download_it_once(tool, release):
    results, errors = [], []

    def want():
        try:
            results.append(tool.executable(str(release.cache)))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=want) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(results)) == 1
    assert len(release.downloads) == 1


def test_a_lock_left_by_a_dead_process_is_broken(tool, release):
    directory = release.cache / "cadcraft" / FAKE_VERSION / release.key
    directory.mkdir(parents=True)
    lock = directory / ".lock"
    lock.write_text("12345")
    old = time.time() - tool.STALE_LOCK - 10
    os.utime(lock, (old, old))
    assert os.path.exists(tool.executable(str(release.cache)))
    assert not lock.exists()


@posix_only
def test_what_the_tool_says_is_passed_on_and_its_failures_raised(tool, release, tmp_path):
    cache = str(release.cache)
    assert tool.run(["--version"], cache_dir=cache).stdout.strip() == "cadcraft-cli 9.9.9"

    source, target = tmp_path / "a.dxf", tmp_path / "b.dwg"
    source.write_text("drawing")
    # "wrote ..." is success, not news; anything else is passed on.
    assert tool.convert(str(source), str(target), cache_dir=cache) == ["note: 1 entity dropped"]
    with pytest.raises(tool.CadcraftError, match=r"exit 3\): boom"):
        tool.run(["fail"], cache_dir=cache)


@pytest.mark.skipif(not os.environ.get("CADCRAFT_NETWORK_TESTS"), reason="set CADCRAFT_NETWORK_TESTS=1 to ask GitHub")
def test_the_pinned_checksums_are_the_release_s_own(tool):
    url = tool.RELEASE_URL.format(version=tool.VERSION, asset=tool.CHECKSUMS_ASSET)
    with urllib.request.urlopen(url, context=tool._ssl_context(), timeout=60) as response:
        published = dict(reversed(line.split()) for line in response.read().decode().splitlines() if line.strip())
    for asset, digest in tool.PINNED_SHA256.items():
        assert published[asset] == digest, asset
