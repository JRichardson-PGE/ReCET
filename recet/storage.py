"""Where a worker's files come from and go to: a local path (or file:// URI) or an S3 object (s3://bucket/key). The engine itself never imports boto3; only
`S3Storage` does, when an s3:// URI is used (`pip install ".[s3]"`). Credentials, region and endpoint are boto3's own (environment, instance role,
AWS_ENDPOINT_URL), never part of a job spec."""
from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


class StorageError(RuntimeError):
    """A file could not be read or written; the message names the URI."""


class LocalStorage:
    """Paths on this machine (or a mounted file system)."""

    @staticmethod
    def path(uri: str) -> Path:
        if uri.startswith("file://"):
            p = urlparse(uri)
            path = p.path if not p.netloc or p.netloc == "localhost" else "//" + p.netloc + p.path   # file://host/share/... is a UNC path
            return Path(url2pathname(path))       # on Windows /C:/Users/... becomes C:\Users\...; elsewhere only the %-escapes are undone
        return Path(uri)

    def fetch(self, uri: str, dest: Path) -> Path:
        src = self.path(uri)
        if not src.is_file():
            raise StorageError(f"input not found: {uri}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    def put(self, src: Path, uri: str) -> None:
        dest = self.path(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


class S3Storage:
    """Objects in S3 (or an S3-compatible store through AWS_ENDPOINT_URL)."""

    def __init__(self, client=None):
        if client is None:
            try:
                import boto3
            except ImportError as exc:                        # pragma: no cover - depends on the install
                raise StorageError('s3:// URIs need boto3: pip install "recet[s3]"') from exc
            client = boto3.client("s3")
        self.client = client

    @staticmethod
    def split(uri: str) -> tuple[str, str]:
        p = urlparse(uri)
        if p.scheme != "s3" or not p.netloc:
            raise StorageError(f"not an s3://bucket/key URI: {uri}")
        return p.netloc, p.path.lstrip("/")

    def fetch(self, uri: str, dest: Path) -> Path:
        bucket, key = self.split(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(bucket, key, str(dest))
        except Exception as exc:
            raise StorageError(f"could not read {uri}: {exc}") from exc
        return dest

    def put(self, src: Path, uri: str) -> None:
        bucket, key = self.split(uri)
        try:
            self.client.upload_file(str(src), bucket, key)
        except Exception as exc:
            raise StorageError(f"could not write {uri}: {exc}") from exc


_S3: S3Storage | None = None


def for_uri(uri: str):
    """The storage that serves `uri`: S3Storage for s3://, LocalStorage for a path or file:// (anything else is refused)."""
    global _S3
    scheme = urlparse(uri).scheme
    if scheme == "s3":
        if _S3 is None:
            _S3 = S3Storage()
        return _S3
    if scheme in ("", "file") or (len(scheme) == 1 and scheme.isalpha()):      # a Windows drive letter parses as a scheme
        return LocalStorage()
    raise StorageError(f"unsupported URI scheme {scheme!r} in {uri} (local paths, file:// and s3:// are supported)")


def join(base: str, name: str) -> str:
    """`base` (a folder URI or path) / `name`."""
    if urlparse(base).scheme == "s3":
        return base.rstrip("/") + "/" + name
    return str(LocalStorage.path(base) / name)


def put_dir(local: Path, uri: str) -> list[str]:
    """Copy every file under `local` to the folder `uri`, keeping relative paths; returns the URIs written."""
    out = []
    for f in sorted(p for p in Path(local).rglob("*") if p.is_file()):
        dest = join(uri, f.relative_to(local).as_posix())
        for_uri(dest).put(f, dest)
        out.append(dest)
    return out
