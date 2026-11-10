#!/usr/bin/env python3
"""Normalize Unix permissions inside the Linux release zip.

flutter_distributor's zip target stores every entry with a FAT-style external
attribute (mode 0600), so `unzip` produces a non-executable `simple_live_app`.
This script rewrites the archive with proper Unix modes so the app can be
started right after extraction.

Usage: fix_zip_permissions.py <archive.zip> [more.zip ...]
"""

import os
import stat
import sys
import tempfile
import zipfile

APP_BINARY = "simple_live_app"


def fix_archive(path):
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(prefix=".fixzip-", suffix=".zip", dir=directory)
    os.close(handle)
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True
        ) as target:
            for info in source.infolist():
                is_directory = info.filename.endswith("/")
                if is_directory:
                    mode = 0o755
                elif info.filename == APP_BINARY:
                    mode = 0o755
                else:
                    mode = 0o644

                entry = zipfile.ZipInfo(info.filename, info.date_time)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3  # Unix, so unzip honors external_attr
                entry.external_attr = (
                    (stat.S_IFDIR | mode) << 16
                    if is_directory
                    else (stat.S_IFREG | mode) << 16
                )
                entry.internal_attr = info.internal_attr
                entry.comment = info.comment
                if is_directory:
                    target.writestr(entry, b"")
                else:
                    target.writestr(entry, source.read(info.filename))
        os.replace(temporary, path)
        print(f"fixed permissions: {path}")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <archive.zip> [...]", file=sys.stderr)
        return 2
    for path in sys.argv[1:]:
        fix_archive(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
