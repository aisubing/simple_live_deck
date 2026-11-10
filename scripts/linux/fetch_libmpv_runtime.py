#!/usr/bin/env python3
"""Fetch a portable libmpv runtime for bundling into the Linux release.

media_kit does not ship libmpv on Linux: the built plugin links against the
system ``libmpv.so.2``.  SteamOS (Steam Deck) is immutable and does not ship
libmpv (nor libxml2), so the release bundle must carry its own copy.

This script downloads libmpv2 and its full runtime dependency closure from
Debian 12 (bookworm).  Debian 12 binaries only require glibc >= 2.34, which is
older than what the Flutter app itself requires, so the result runs on every
current SteamOS / desktop distribution.

The result is written to ``third_party/libmpv-linux/runtime`` as flat shared
libraries named by the exact NEEDED soname, each with an ``$ORIGIN`` RUNPATH.
``scripts/linux/inject_libmpv_runtime.sh`` copies that directory into the
Flutter bundle before packaging.

Requirements: python3 (stdlib only), dpkg-deb, patchelf.

Environment:
  DEBIAN_MIRROR  Debian mirror (default https://deb.debian.org/debian)
  LIBMPV_FORCE   set to 1 to rebuild even if the runtime is already present
"""

import concurrent.futures
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MIRROR = os.environ.get("DEBIAN_MIRROR", "https://deb.debian.org/debian").rstrip("/")
SUITE = os.environ.get("DEBIAN_SUITE", "bookworm")
ARCH = "amd64"
FORCE = os.environ.get("LIBMPV_FORCE", "") == "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "third_party" / "libmpv-linux" / "runtime"
MARKER = RUNTIME_DIR / ".complete"

# Packages provided by the base system (SteamOS / desktop distributions).
# Their libraries are resolved at runtime from the host and must never be
# bundled: bundling glibc or the GL/GTK stack breaks the host application.
SKIP_PACKAGES = set(
    """
    libc6 libgcc-s1 libstdc++6 libcrypt1
    libglib2.0-0 libgobject-2.0-0 libgio-2.0-0 libgmodule-2.0-0 libgthread-2.0-0
    libgtk-3-0 libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0
    libcairo2 libcairo-gobject2 libgdk-pixbuf-2.0-0
    libharfbuzz0b libfreetype6 libfontconfig1 libexpat1 libffi8 libpcre2-8-0
    libdbus-1-3 libsystemd0 libudev1 libselinux1 libmount1 libblkid1
    libcap2 libcap-ng0 liblz4-1 libdeflate0 libzstd1 liblzma5 libbz2-1.0
    zlib1g libpng16-16 libssl3 libcrypto3
    libasound2 libpulse0 libpipewire-0.3-0
    libgl1 libglx0 libglvnd0 libegl1 libgles2 libopengl0
    libdrm2 libgbm1 libvulkan1 libepoxy0
    libwayland-client0 libwayland-cursor0 libwayland-egl1 libxkbcommon0
    libx11-6 libx11-xcb1 libxcb1 libxau6 libxdmcp6 libxi6 libxcursor1
    libxfixes3 libxrender1 libxext6 libxrandr2 libsm6 libice6
    libncursesw6 libtinfo6
    """.split()
)

# Same boundary expressed as NEEDED sonames, used while walking the shared
# library graph and when filtering extracted files.
SKIP_SONAMES = [
    "libc.so", "libm.so", "libdl.so", "libpthread.so", "librt.so",
    "libresolv.so", "libutil.so", "libcrypt.so", "libgcc_s.so",
    "libstdc++.so", "ld-linux",
    "libglib-2.0", "libgobject-2.0", "libgio-2.0", "libgmodule-2.0",
    "libgthread-2.0", "libgtk-3", "libgdk-3", "libpango", "libpangocairo",
    "libcairo", "libgdk_pixbuf", "libharfbuzz", "libfreetype",
    "libfontconfig", "libexpat", "libffi", "libpcre2", "libdbus-1",
    "libsystemd", "libudev", "libselinux", "libmount", "libblkid",
    "libcap", "liblz4", "libdeflate", "libz.so", "libzstd", "liblzma",
    "libbz2", "libpng", "libssl", "libcrypto", "libasound", "libpulse",
    "libpipewire", "libGL", "libEGL", "libGLES", "libOpenGL", "libglvnd",
    "libvulkan", "libdrm", "libgbm", "libwayland", "libxkbcommon",
    "libX11", "libxcb", "libXau", "libXdmcp", "libXi.so", "libXcursor",
    "libXfixes", "libXrender", "libXext", "libXrandr", "libSM.so",
    "libICE.so", "libncurses", "libtinfo", "libepoxy",
]

ROOT_PACKAGE = "libmpv2"
START_SONAME = "libmpv.so.2"


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def is_skipped(soname):
    return any(soname.startswith(prefix) for prefix in SKIP_SONAMES)


def script_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def check_tools():
    missing = [
        tool
        for tool in ("dpkg-deb", "patchelf", "readelf")
        if shutil.which(tool) is None
    ]
    if missing:
        fail(
            "missing required tools: "
            + ", ".join(missing)
            + "\n  Debian/Ubuntu: sudo apt install patchelf dpkg binutils"
        )


def download(url, path, retries=4):
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                with open(str(path) + ".part", "wb") as handle:
                    shutil.copyfileobj(response, handle, length=1 << 20)
            os.replace(str(path) + ".part", path)
            return
        except Exception as error:  # noqa: BLE001 - retried below
            last = error
    fail(f"cannot download {url}: {last}")


def load_index(cache_dir):
    index_path = cache_dir / "Packages.gz"
    url = f"{MIRROR}/dists/{SUITE}/main/binary-{ARCH}/Packages.gz"
    print(f"fetching {url}")
    download(url, index_path)

    packages = {}
    provides = {}
    current = {}

    def flush():
        if not current:
            return
        name = current.get("Package")
        if name:
            packages[name] = current.copy()
            for provided in current.get("Provides", "").split(","):
                provided = provided.strip().split("(")[0].strip()
                if provided:
                    provides.setdefault(provided, []).append(name)
        current.clear()

    with gzip.open(index_path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                flush()
                continue
            if line[0] in " \t":
                continue
            key, _, value = line.partition(":")
            if key in ("Depends", "Pre-Depends"):
                current[key] = (current.get(key, "") + "," + value.strip()).strip(",")
            else:
                current[key] = value.strip()
    flush()
    return packages, provides


def parse_dependencies(value):
    groups = []
    for part in value.split(","):
        alternatives = []
        for alternative in part.split("|"):
            name = alternative.strip().split("(")[0].strip().split(":")[0].strip()
            if name:
                alternatives.append(name)
        if alternatives:
            groups.append(alternatives)
    return groups


def pick_package(packages, provides, name):
    if name in packages:
        return name
    candidates = provides.get(name)
    if candidates:
        for candidate in candidates:
            if candidate not in SKIP_PACKAGES:
                return candidate
    return None


def resolve(packages, provides):
    selected = {}
    pending = [ROOT_PACKAGE]
    missing = set()
    while pending:
        name = pending.pop()
        if name in selected or name in SKIP_PACKAGES:
            continue
        package = pick_package(packages, provides, name)
        if package is None:
            missing.add(name)
            continue
        selected[package] = packages[package]
        deps = (packages[package].get("Pre-Depends", "") + "," + packages[package].get("Depends", "")).strip(",")
        for group in parse_dependencies(deps):
            for alternative in group:
                if alternative in SKIP_PACKAGES:
                    break
                chosen = pick_package(packages, provides, alternative)
                if chosen is not None:
                    if chosen not in selected:
                        pending.append(chosen)
                    break
    if missing:
        fail("unresolved Debian dependencies: " + ", ".join(sorted(missing)))
    return list(selected.values())


def download_packages(packages, cache_dir):
    debs = cache_dir / "debs"
    debs.mkdir(parents=True, exist_ok=True)

    def fetch(package):
        filename = package["Filename"]
        name = os.path.basename(filename)
        target = debs / name
        expected = package.get("SHA256")
        if target.exists() and expected:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == expected:
                return target
            target.unlink()
        download(f"{MIRROR}/{filename}", target)
        if expected:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != expected:
                fail(f"checksum mismatch for {name}")
        return target

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(fetch, packages))


def elf_info(path):
    output = subprocess.run(
        ["readelf", "-d", path], capture_output=True, text=True, check=False
    ).stdout
    soname = None
    needed = []
    for line in output.splitlines():
        if "(SONAME)" in line:
            soname = line.split("[")[-1].rstrip("]")
        match = re.search(r"Shared library: \[(.*?)\]", line)
        if match:
            needed.append(match.group(1))
    return soname, needed


def collect_libraries(extract_root, output_dir):
    by_name = {}
    entries = {}

    for base in ("lib", "usr/lib"):
        pattern = os.path.join(str(extract_root), "*", base, "x86_64-linux-gnu", "**", "*.so*")
        for path in glob.glob(pattern, recursive=True):
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            if not os.path.isfile(real):
                continue
            soname, needed = elf_info(real)
            if not soname or is_skipped(soname):
                continue
            entry = entries.setdefault(
                soname, {"path": real, "soname": soname, "needed": needed, "names": set()}
            )
            entry["names"].add(os.path.basename(path))
            by_name[os.path.basename(path)] = entry
            by_name.setdefault(soname, entry)

    if START_SONAME not in by_name:
        fail(f"libmpv not found in downloaded packages (expected {START_SONAME})")

    keep = {}
    pending = [START_SONAME]
    unresolved = set()
    while pending:
        name = pending.pop()
        entry = by_name.get(name)
        if entry is None:
            unresolved.add(name)
            continue
        soname = entry["soname"]
        if soname in keep:
            keep[soname]["names"].add(name)
            continue
        keep[soname] = entry
        pending.extend(entry["needed"])

    unexpected = sorted(name for name in unresolved if not is_skipped(name) and name != "ld-linux-x86-64.so.2")
    if unexpected:
        fail(
            "unresolved shared libraries that are not part of the base system: "
            + ", ".join(unexpected)
            + "\n  Add their Debian packages to the resolution set or extend SKIP_SONAMES."
        )

    required = {START_SONAME}
    for entry in keep.values():
        required.update(entry["needed"])

    output_dir.mkdir(parents=True, exist_ok=True)
    file_count = 0
    for soname, entry in sorted(keep.items()):
        names = {soname} | {name for name in required if by_name.get(name) is entry}
        for name in names:
            target = output_dir / name
            if target.exists():
                target.unlink()
            shutil.copy2(entry["path"], target)
            subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", target], check=True)
            file_count += 1

    patched = sorted(keep)
    print(f"bundled {len(patched)} libraries ({file_count} files)")
    print("system libraries left to the host: " + ", ".join(sorted(unresolved)))
    return patched


def main():
    check_tools()

    digest = script_digest()
    if MARKER.exists() and not FORCE:
        try:
            marker = json.loads(MARKER.read_text())
        except (OSError, ValueError):
            marker = {}
        if marker.get("script") == digest and (RUNTIME_DIR / START_SONAME).exists():
            print(f"runtime already prepared: {RUNTIME_DIR}")
            return

    workdir = Path(tempfile.mkdtemp(prefix="libmpv-runtime-"))
    try:
        cache_dir = workdir / "cache"
        cache_dir.mkdir()
        packages, provides = load_index(cache_dir)
        selected = resolve(packages, provides)
        print(f"resolved {len(selected)} Debian packages")

        debs = download_packages(selected, cache_dir)
        print(f"downloaded {len(debs)} packages")

        extract_root = workdir / "extract"
        extract_root.mkdir()
        for deb in debs:
            subprocess.run(
                ["dpkg-deb", "-x", str(deb), str(extract_root / deb.stem)],
                check=True,
            )

        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        collect_libraries(extract_root, RUNTIME_DIR)

        MARKER.write_text(
            json.dumps(
                {
                    "script": digest,
                    "mirror": MIRROR,
                    "suite": SUITE,
                    "packages": len(selected),
                },
                indent=2,
            )
            + "\n"
        )
        print(f"done: {RUNTIME_DIR}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
