"""Local, dependency-free storage operations for mounted NAS shares (Python 3.10+)."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import time

VERSION = "0.1.1"
DEFAULT_CONFIG = Path.home() / ".config" / "synology-storage" / "config.json"


class StorageError(Exception):
    pass


def load_config(path):
    with open(path, encoding="utf-8-sig") as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or not isinstance(config.get("roots"), dict) or not config["roots"]:
        raise StorageError("Configuration must contain a nonempty roots object.")
    for alias, spec in config["roots"].items():
        if not isinstance(alias, str) or not isinstance(spec, dict):
            raise StorageError("Invalid root configuration.")
        if not isinstance(spec.get("path"), str) or not Path(spec["path"]).is_absolute():
            raise StorageError("Root paths must be absolute.")
        if type(spec.get("writable", False)) is not bool:
            raise StorageError("writable must be a boolean.")
    return config


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def resolve_path(config, alias, relative=".", write=False):
    spec = config["roots"].get(alias)
    if spec is None:
        raise StorageError("Unknown root alias.")
    if write and not spec.get("writable", False):
        raise StorageError("This root is read-only.")
    # Reject alternate streams, drive-relative paths, traversal and Windows aliases on every OS.
    relative = relative.replace("\\", "/")
    parts = relative.split("/")
    if relative.startswith("/") or PureWindowsPath(relative).drive or ":" in relative:
        raise StorageError("Use a relative path inside a configured root.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    for part in parts:
        if part == ".." or (part not in ("", ".") and (
            part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
            or any(ord(c) < 32 or c in '<>"|?*' for c in part)
        )):
            raise StorageError("Unsafe relative path.")
    root = Path(spec["path"])
    if not root.is_dir():
        raise StorageError("Root unavailable; check the mounted share and permissions.")
    # The configured root is trusted, but descendants must not redirect elsewhere.
    root = root.resolve(strict=True)
    target = root
    for part in parts:
        if part in ("", "."):
            continue
        target = target / part
        if target.exists() or target.is_symlink():
            if is_link(target):
                raise StorageError("Symbolic links and reparse points are not supported.")
    if not target.resolve().is_relative_to(root):
        raise StorageError("Path escapes the configured root.")
    return target


def fingerprint(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("A regular file is required.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def digest(path):
    before = fingerprint(path)
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    if fingerprint(path) != before:
        raise StorageError("File changed during verification; retry when idle.")
    return value.hexdigest()


def scan(root, pattern, limit, max_entries, recursive):
    results, pending, examined, skipped = [], [root], 0, 0
    truncated = False
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    examined += 1
                    if examined > max_entries:
                        return dict(items=results, truncated=True, examined=examined - 1, skipped=skipped)
                    path = Path(entry.path)
                    try:
                        if is_link(path):
                            skipped += 1
                            continue
                        info = entry.stat(follow_symlinks=False)
                        is_dir = stat.S_ISDIR(info.st_mode)
                        if not is_dir and not stat.S_ISREG(info.st_mode):
                            skipped += 1
                            continue
                        if is_dir and recursive:
                            pending.append(path)
                        if fnmatch.fnmatchcase(entry.name.casefold(), pattern.casefold()):
                            results.append(dict(path=path.relative_to(root).as_posix(),
                                                kind="directory" if is_dir else "file",
                                                bytes=None if is_dir else info.st_size,
                                                modified=info.st_mtime))
                            if len(results) >= limit:
                                return dict(items=results, truncated=True, examined=examined, skipped=skipped)
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    return dict(items=results, truncated=truncated, examined=examined, skipped=skipped)


def copy_verified(source, target, apply=False):
    before = fingerprint(source)
    if source == target:
        raise StorageError("Source and destination must differ.")
    if target.exists():
        raise StorageError("Destination already exists; choose another name. Nothing overwritten.")
    if not target.parent.is_dir():
        raise StorageError("Destination parent does not exist; create it first.")
    if not apply:
        return dict(applied=False, bytes=before[2], operation="copy", overwrite=False)
    # Exclusive create also protects against a destination appearing after the check.
    # Never delete the source. On failure preserve the destination for inspection.
    expected = hashlib.sha256()
    with target.open("xb") as output:
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                expected.update(block)
                output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if fingerprint(source) != before or digest(source) != expected.hexdigest():
        raise StorageError("Source changed; destination is unverified. Both files retained.")
    if digest(target) != expected.hexdigest():
        raise StorageError("Destination verification failed. Both files retained.")
    return dict(applied=True, verified=True, bytes=before[2], sha256=expected.hexdigest())


def run(config, args):
    if args.command == "status":
        root = resolve_path(config, args.root)
        usage = shutil.disk_usage(root)
        return dict(root=args.root, available=True, total_bytes=usage.total,
                    free_bytes=usage.free, used_bytes=usage.used,
                    writable_configured=config["roots"][args.root].get("writable", False),
                    disk_health="unknown", raid_health="unknown")
    if args.command == "sync-status":
        configured = config.get("sync_status_file")
        if not configured:
            raise StorageError("No sync_status_file configured.")
        path = Path(configured)
        if not path.is_absolute():
            raise StorageError("sync_status_file must be absolute.")
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise StorageError("Status file exceeds 1 MiB.")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise StorageError("Status must be a JSON object.")
        # Do not echo error strings, which can contain private hostnames or paths.
        public = {key: data[key] for key in ("phase", "started", "updated", "copied", "freed_bytes") if key in data}
        errors = data.get("errors", [])
        if not isinstance(errors, list):
            raise StorageError("Status errors must be an array.")
        public["error_count"] = len(errors)
        public["age_seconds"] = max(0, round(time.time() - path.stat().st_mtime))
        public["stale"] = public["age_seconds"] > args.max_age
        public["source"] = "external_status_file"
        public["backup_integrity"] = "not_checked"
        return public
    path = resolve_path(config, args.root, args.path, write=args.command == "mkdir")
    if args.command in ("list", "search"):
        if not path.is_dir():
            raise StorageError("A directory is required.")
        return scan(path, getattr(args, "pattern", "*"), args.limit, args.max_entries, args.command == "search")
    if args.command == "hash":
        return dict(root=args.root, path=args.path, sha256=digest(path))
    if args.command == "mkdir":
        for candidate in (path, *path.parents):
            if candidate.exists() and not candidate.is_dir():
                raise StorageError("Directory path is blocked by an existing file.")
        if not args.apply:
            return dict(applied=False, operation="mkdir", exists=path.exists())
        path.mkdir(parents=True, exist_ok=True)
        return dict(applied=True, operation="mkdir")
    target = resolve_path(config, args.to_root, args.to_path, write=args.command == "copy")
    if args.command == "verify":
        first, second = digest(path), digest(target)
        return dict(verified=first == second, source_sha256=first, destination_sha256=second)
    return copy_verified(path, target, args.apply)


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("Must be positive.")
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--version", action="version", version=VERSION)
    cli.add_argument("--config", default=os.environ.get("SYNOLOGY_STORAGE_CONFIG", str(DEFAULT_CONFIG)))
    cli.add_argument("--timeout", type=positive, default=60, help="Wall-clock limit in seconds (default 60)")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("status", "list", "search", "hash", "mkdir", "copy", "verify", "sync-status"):
        cmd = commands.add_parser(name)
        if name == "sync-status":
            cmd.add_argument("--max-age", type=positive, default=1800)
            continue
        cmd.add_argument("--root", required=True)
        if name != "status":
            cmd.add_argument("--path", default=".")
        if name in ("list", "search"):
            cmd.add_argument("--limit", type=positive, default=100)
            cmd.add_argument("--max-entries", type=positive, default=10000)
        if name == "search":
            cmd.add_argument("--pattern", required=True, help="Case-insensitive filename glob")
        if name in ("copy", "verify"):
            cmd.add_argument("--to-root", required=True)
            cmd.add_argument("--to-path", required=True)
        if name in ("copy", "mkdir"):
            cmd.add_argument("--apply", action="store_true", help="Perform the requested write")
    return cli


def emit(value):
    print(json.dumps(value, ensure_ascii=True))


def main():
    worker = len(sys.argv) > 1 and sys.argv[1] == "__worker"
    argv = sys.argv[2:] if worker else sys.argv[1:]
    args = parser().parse_args(argv)
    if not worker:
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "__worker", *argv],
                                    capture_output=True, timeout=args.timeout)
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            return result.returncode
        except subprocess.TimeoutExpired:
            emit(dict(ok=False, error="Operation timed out; availability is unknown. A copy may be incomplete; source retained."))
            return 2
    try:
        data = run(load_config(args.config), args)
        emit(dict(ok=True, **data))
        return 1 if data.get("verified") is False else 0
    except StorageError as exc:
        emit(dict(ok=False, error=str(exc)))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        emit(dict(ok=False, error=type(exc).__name__ + ": operation failed; check local configuration, access and file state. A failed copy may leave an incomplete destination."))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
