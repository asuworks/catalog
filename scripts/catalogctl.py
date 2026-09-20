#!/usr/bin/env python3
"""Candidate-centered deployment controller for Catalog hosts."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import fcntl
import getpass
import hashlib
import io
import json
import lzma
import os
import pwd
import re
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator


SCHEMA_VERSION = 1
CONTROLLER_VERSION = 1
DATABASE_NAME = "comses_catalog"
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOST_IDS = {"staging", "prod"}
HOST_DOMAINS = {
    "staging": "staging-catalog.comses.net",
    "prod": "catalog.comses.net",
}
HOST_HELPER = Path("/usr/local/libexec/comses-catalog")
MANAGED_ALIASES = {
    "publication",
    "publication_curator",
    "author",
    "container",
    "model_documentation",
    "platform",
    "sponsor",
    "tag",
}


class CatalogError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path, *, excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        metadata = path.lstat()
        if path.is_symlink():
            digest.update(f"L {relative}\0{os.readlink(path)}\0".encode())
        elif path.is_dir():
            digest.update(f"D {relative}\0".encode())
        elif path.is_file():
            digest.update(f"F {relative}\0{metadata.st_mode & 0o111:o}\0".encode())
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise CatalogError(f"unsupported file in release bundle: {path}")
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(path, payload, mode)


def read_json(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise CatalogError(f"missing required state file: {path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"invalid JSON state file {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(f"unsupported state schema in {path}")
    return value


def require_json(path: Path) -> dict[str, Any]:
    value = read_json(path, required=True)
    assert value is not None
    return value


def append_json_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def validate_identifier(value: str) -> str:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise CatalogError(f"unsafe PostgreSQL identifier: {value}")
    return value


def validate_image(value: str) -> str:
    if not IMAGE_RE.fullmatch(value):
        raise CatalogError("IMAGE must be a complete name@sha256:<64 lowercase hex> reference")
    return value


def validate_revision(value: str) -> str:
    if not REVISION_RE.fullmatch(value):
        raise CatalogError("BUNDLE_REVISION must be a full 40-character lowercase Git SHA")
    return value


@dataclass(frozen=True)
class Layout:
    etc: Path
    var: Path
    backups: Path

    @classmethod
    def from_environment(cls) -> "Layout":
        return cls(
            Path(os.environ.get("CATALOG_ETC_DIR", "/etc/comses-catalog")).resolve(),
            Path(os.environ.get("CATALOG_STATE_DIR", "/var/lib/comses-catalog")).resolve(),
            Path(os.environ.get("CATALOG_BACKUP_DIR", "/var/backups/comses-catalog")).resolve(),
        )

    @property
    def host_env(self) -> Path:
        return self.etc / "host.env"

    @property
    def secrets(self) -> Path:
        return self.etc / "secrets"

    @property
    def state(self) -> Path:
        return self.var / "state"

    @property
    def runtime(self) -> Path:
        return self.var / "runtime"

    @property
    def releases(self) -> Path:
        return self.var / "releases"

    @property
    def shared(self) -> Path:
        return self.var / "shared"

    @property
    def receipts(self) -> Path:
        return self.var / "receipts"

    @property
    def reports(self) -> Path:
        return self.var / "reports"

    @property
    def lock(self) -> Path:
        return self.var / "catalog.lock"

    @property
    def active(self) -> Path:
        return self.state / "active.json"

    @property
    def rollback(self) -> Path:
        return self.state / "rollback.json"

    @property
    def candidate(self) -> Path:
        return self.state / "candidate.json"

    @property
    def journal(self) -> Path:
        return self.state / "journal.json"

    @property
    def history(self) -> Path:
        return self.state / "history.jsonl"

    @property
    def canonical_compose(self) -> Path:
        return self.runtime / "docker-compose.yml"


@dataclass(frozen=True)
class HostConfig:
    host_id: str
    environment: str
    project_name: str
    domain: str
    http_bind: str

    @classmethod
    def load(cls, layout: Layout) -> "HostConfig":
        values: dict[str, str] = {}
        try:
            lines = layout.host_env.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise CatalogError(f"cannot read {layout.host_env}: {error}") from error
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise CatalogError(f"malformed host identity line: {line}")
            key, value = line.split("=", 1)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not value:
                raise CatalogError(f"malformed host identity line: {line}")
            values[key] = value
        required = {
            "CATALOG_HOST_ID",
            "CATALOG_ENV",
            "COMPOSE_PROJECT_NAME",
            "CATALOG_DOMAIN",
            "CATALOG_HTTP_BIND",
        }
        missing = sorted(required.difference(values))
        if missing:
            raise CatalogError(f"host identity is missing: {', '.join(missing)}")
        host_id = values["CATALOG_HOST_ID"]
        if host_id not in HOST_IDS or values["CATALOG_ENV"] != host_id:
            raise CatalogError("CATALOG_HOST_ID and CATALOG_ENV must be the same staging or prod value")
        if values["COMPOSE_PROJECT_NAME"] != "catalog":
            raise CatalogError("COMPOSE_PROJECT_NAME must be catalog")
        if values["CATALOG_DOMAIN"] != HOST_DOMAINS[host_id]:
            raise CatalogError(f"CATALOG_DOMAIN does not match fixed {host_id} identity")
        return cls(
            host_id,
            values["CATALOG_ENV"],
            values["COMPOSE_PROJECT_NAME"],
            values["CATALOG_DOMAIN"],
            values["CATALOG_HTTP_BIND"],
        )


class Runner:
    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_data: bytes | str | None = None,
        capture: bool = True,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_data,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=check,
            text=text,
        )

    def to_file(
        self,
        command: list[str],
        destination: BinaryIO,
        *,
        cwd: Path | None = None,
    ) -> None:
        subprocess.run(command, cwd=cwd, stdout=destination, check=True)

    def from_file(
        self,
        command: list[str],
        source: BinaryIO,
        *,
        cwd: Path | None = None,
    ) -> None:
        subprocess.run(command, cwd=cwd, stdin=source, check=True)


class Controller:
    def __init__(self, root: Path, layout: Layout, runner: Runner | None = None):
        self.root = root.resolve()
        self.layout = layout
        self.runner = runner or Runner()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.layout.var.mkdir(parents=True, exist_ok=True)
        with self.layout.lock.open("a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def command_output(self, command: list[str], *, cwd: Path | None = None) -> str:
        try:
            result = self.runner.run(command, cwd=cwd)
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip() if isinstance(error.stderr, str) else ""
            raise CatalogError(f"command failed: {' '.join(command)}{': ' + stderr if stderr else ''}") from error
        return (result.stdout or "").strip()

    def host(self) -> HostConfig:
        host = HostConfig.load(self.layout)
        self.require_safe_file(self.layout.host_env, secret=False)
        return host

    @staticmethod
    def require_safe_file(path: Path, *, secret: bool) -> None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise CatalogError(f"cannot inspect {path}: {error}") from error
        forbidden = 0o077 if secret else 0o022
        if mode & forbidden:
            expected = "owner-only" if secret else "not group/other writable"
            raise CatalogError(f"unsafe permissions on {path}: expected {expected}")
        if not path.is_file() or path.is_symlink():
            raise CatalogError(f"expected a regular file: {path}")

    def host_provision(self, host_id: str, operator: str) -> None:
        if host_id not in HOST_IDS:
            raise CatalogError("HOST_ID must be staging or prod")
        if self.layout.etc == Path("/etc/comses-catalog") and os.geteuid() != 0:
            raise CatalogError("host-provision for system paths must run as root")
        try:
            operator_record = pwd.getpwnam(operator)
        except KeyError as error:
            raise CatalogError(f"unknown deployment operator: {operator}") from error

        directories = (
            self.layout.etc,
            self.layout.secrets,
            self.layout.var,
            self.layout.state,
            self.layout.runtime,
            self.layout.releases,
            self.layout.shared,
            self.layout.shared / "catalog" / "logs",
            self.layout.shared / "logs",
            self.layout.shared / "mail",
            self.layout.shared / "nginx" / "logs",
            self.layout.receipts / "backups",
            self.layout.receipts / "restores",
            self.layout.reports,
            self.layout.backups,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            if os.geteuid() == 0:
                os.chown(directory, operator_record.pw_uid, operator_record.pw_gid)

        if self.layout.host_env.exists():
            existing = HostConfig.load(self.layout)
            if existing.host_id != host_id:
                raise CatalogError("host identity is immutable and does not match HOST_ID")
        else:
            host_payload = (
                f"CATALOG_HOST_ID={host_id}\n"
                f"CATALOG_ENV={host_id}\n"
                "COMPOSE_PROJECT_NAME=catalog\n"
                f"CATALOG_DOMAIN={HOST_DOMAINS[host_id]}\n"
                "CATALOG_HTTP_BIND=127.0.0.1:80\n"
            )
            atomic_write(self.layout.host_env, host_payload.encode(), 0o640)

        password_path = self.layout.secrets / "postgres_password"
        config_path = self.layout.secrets / "config.ini"
        if not password_path.exists() and not config_path.exists():
            import secrets

            password = secrets.token_urlsafe(45)
            secret_key = secrets.token_urlsafe(75)
            template_path = self.root / "deploy" / "conf" / "config.template.ini"
            template = string.Template(template_path.read_text(encoding="utf-8"))
            config = template.substitute(
                DB_NAME=DATABASE_NAME,
                DB_USER="catalog",
                DB_PASSWORD=password,
                SECRET_KEY=secret_key,
            )
            atomic_write(password_path, f"{password}\n".encode(), 0o600)
            atomic_write(config_path, config.encode(), 0o600)
        elif not password_path.exists() or not config_path.exists():
            raise CatalogError("secret configuration is incomplete; refusing to replace either file")

        if os.geteuid() == 0:
            for path in (self.layout.host_env, password_path, config_path):
                os.chown(path, operator_record.pw_uid, operator_record.pw_gid)
            os.chmod(self.layout.host_env, 0o640)
            os.chmod(password_path, 0o600)
            os.chmod(config_path, 0o600)
            self.install_host_helper(operator)
            sysctl = Path("/etc/sysctl.d/99-comses-catalog.conf")
            atomic_write(sysctl, b"vm.max_map_count=262144\n", 0o644)
            self.runner.run(["sysctl", "--system"], capture=False)
        print(f"Provisioned {host_id} host state under {self.layout.var}")
        print(f"Review {config_path} before running host-check")

    def install_host_helper(self, operator: str) -> None:
        helper = HOST_HELPER
        helper.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(helper, Path(__file__).read_bytes(), 0o755)
        service = f"""[Unit]
Description=CoMSES Catalog PostgreSQL backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User={operator}
ExecStart={helper} backup --scheduled
"""
        timer = """[Unit]
Description=Nightly CoMSES Catalog PostgreSQL backup

[Timer]
OnCalendar=*-*-* 02:15:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
"""
        atomic_write(Path("/etc/systemd/system/comses-catalog-backup.service"), service.encode(), 0o644)
        atomic_write(Path("/etc/systemd/system/comses-catalog-backup.timer"), timer.encode(), 0o644)
        self.runner.run(["systemctl", "daemon-reload"], capture=False)
        self.runner.run(["systemctl", "enable", "--now", "comses-catalog-backup.timer"], capture=False)

    def host_check(self) -> None:
        host = self.host()
        for secret in (self.layout.secrets / "config.ini", self.layout.secrets / "postgres_password"):
            self.require_safe_file(secret, secret=True)
            if secret.stat().st_size == 0:
                raise CatalogError(f"empty secret file: {secret}")
        parser = configparser.ConfigParser()
        parser.read(self.layout.secrets / "config.ini")
        if not parser.has_option("db", "PASSWORD") or not parser.get("db", "PASSWORD"):
            raise CatalogError("config.ini has no database password")
        database_password = (self.layout.secrets / "postgres_password").read_text(
            encoding="utf-8"
        ).rstrip("\r\n")
        if parser.get("db", "PASSWORD") != database_password:
            raise CatalogError("database passwords differ between config.ini and postgres_password")
        if host.host_id == "prod" and not parser.get("email", "EMAIL_HOST_PASSWORD", fallback=""):
            raise CatalogError("production config.ini requires EMAIL_HOST_PASSWORD")
        self.command_output(["docker", "info"])
        self.command_output(["docker", "compose", "version"])
        if sys.version_info < (3, 10):
            raise CatalogError("Python 3.10 or newer is required")
        max_map = Path("/proc/sys/vm/max_map_count")
        if max_map.exists() and int(max_map.read_text().strip()) < 262144:
            raise CatalogError("vm.max_map_count must be at least 262144")
        if shutil.disk_usage(self.layout.var).free < 10 * 1024**3:
            raise CatalogError(f"less than 10 GiB free under {self.layout.var}")
        if self.layout.etc == Path("/etc/comses-catalog"):
            self.require_safe_file(HOST_HELPER, secret=False)
            if sha256_file(HOST_HELPER) != sha256_file(Path(__file__)):
                raise CatalogError("installed backup helper is stale; rerun host-provision from this checkout")
        read_json(self.layout.journal, required=False)
        print(f"Host check passed: {host.host_id} ({host.domain})")

    def compose_command(self, compose_file: Path, *arguments: str) -> list[str]:
        host = self.host()
        return [
            "docker",
            "compose",
            "--project-directory",
            str(compose_file.parent),
            "-p",
            host.project_name,
            "-f",
            str(compose_file),
            *arguments,
        ]

    def compose(
        self,
        compose_file: Path,
        *arguments: str,
        capture: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            return self.runner.run(
                self.compose_command(compose_file, *arguments),
                capture=capture,
                check=check,
            )
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip() if isinstance(error.stderr, str) else ""
            raise CatalogError(f"Compose command failed: {' '.join(arguments)}{': ' + stderr if stderr else ''}") from error

    def require_clean_revision(self, revision: str) -> str:
        head = self.command_output(["git", "rev-parse", "HEAD"], cwd=self.root)
        if head != revision:
            raise CatalogError(f"checkout HEAD {head} does not match BUNDLE_REVISION {revision}")
        status = self.command_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=self.root
        )
        if status:
            raise CatalogError("candidate creation requires a clean checkout")
        citation_status = self.command_output(
            ["git", "-C", "citation", "status", "--porcelain", "--untracked-files=normal"],
            cwd=self.root,
        )
        if citation_status:
            raise CatalogError("citation submodule must be clean")

        expected_citation = self.command_output(
            ["git", "rev-parse", f"{revision}:citation"], cwd=self.root
        )
        citation_checkout = self.command_output(
            ["git", "-C", "citation", "rev-parse", "HEAD"], cwd=self.root
        )
        if citation_checkout != expected_citation:
            raise CatalogError("citation checkout does not match the Catalog gitlink")
        return expected_citation

    def pull_and_verify_image(
        self, image: str, revision: str, citation_revision: str
    ) -> dict[str, str]:
        self.runner.run(["docker", "pull", image], capture=False)
        labels_raw = self.command_output(
            ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"]
        )
        labels = json.loads(labels_raw or "{}")
        if labels.get("org.opencontainers.image.revision") != revision:
            raise CatalogError("image revision label does not match BUNDLE_REVISION")
        image_citation_revision = labels.get("org.comses.catalog.citation-revision")
        if image_citation_revision != citation_revision:
            raise CatalogError("image Citation revision label does not match the Catalog gitlink")
        return labels

    def archive_release(self, revision: str, release_dir: Path) -> str:
        archive = self.runner.run(
            ["git", "archive", "--format=tar", revision],
            cwd=self.root,
            text=False,
        ).stdout
        temporary = release_dir.with_name(f".{release_dir.name}.{uuid.uuid4().hex}")
        temporary.mkdir(parents=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(temporary, filter="data")
            expected = sha256_tree(temporary)
            if release_dir.exists():
                actual = sha256_tree(release_dir, excluded={"docker-compose.yml"})
                if actual != expected:
                    raise CatalogError(f"existing release bundle was modified: {release_dir}")
                return expected
            os.replace(temporary, release_dir)
            fsync_directory(release_dir.parent)
            return expected
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def candidate_create(self, image: str, revision: str) -> None:
        image = validate_image(image)
        revision = validate_revision(revision)
        with self.locked():
            self.host_check()
            if self.layout.journal.exists():
                raise CatalogError("an unfinished transaction journal exists; run make recover")
            citation_revision = self.require_clean_revision(revision)
            labels = self.pull_and_verify_image(image, revision, citation_revision)
            existing = read_json(self.layout.candidate)
            if existing and (
                existing.get("restore_status") not in {"pending", "not_required"}
                or existing.get("migration_status") != "unmigrated"
                or existing.get("data_rebuild_status") != "pending"
            ):
                raise CatalogError("the existing candidate is locked by restore, migration, or rebuild state")
            digest = image.rsplit("@sha256:", 1)[1]
            release_id = f"{revision[:12]}-{digest[:12]}"
            release_dir = self.layout.releases / release_id
            release_dir.parent.mkdir(parents=True, exist_ok=True)
            bundle_sha256 = self.archive_release(revision, release_dir)
            compose_file = release_dir / "docker-compose.yml"
            host = self.host()
            environment = os.environ.copy()
            environment.update(
                CATALOG_IMAGE=image,
                CATALOG_BUNDLE_REVISION=revision,
                CATALOG_CONFIG_DIR=str(self.layout.secrets),
                CATALOG_SHARED_DIR=str(self.layout.shared),
                CATALOG_BUNDLE_DIR=str(release_dir),
                CATALOG_HTTP_BIND=host.http_bind,
                CATALOG_DOMAIN=host.domain,
                CATALOG_ENV=host.environment,
            )
            self.runner.run(
                ["bash", "scripts/compose.sh", host.environment, str(compose_file)],
                cwd=release_dir,
                env=environment,
                capture=False,
            )
            self.compose(compose_file, "config", "--quiet")
            images_result = self.compose(compose_file, "config", "--images")
            support_images = sorted(
                {line.strip() for line in (images_result.stdout or "").splitlines() if line.strip() != image}
            )
            active = self.active_state()
            if active:
                self.verify_release_artifact(active, "active")
            if active and active.get("support_images") != support_images:
                raise CatalogError(
                    "supporting service image digests changed; use a reviewed infrastructure migration"
                )
            if sha256_tree(release_dir, excluded={"docker-compose.yml"}) != bundle_sha256:
                raise CatalogError("release bundle changed while the candidate was rendered")
            candidate = {
                "schema_version": SCHEMA_VERSION,
                "controller_version": CONTROLLER_VERSION,
                "operation_id": uuid.uuid4().hex,
                "host_id": host.host_id,
                "image": image,
                "bundle_revision": revision,
                "citation_revision": labels["org.comses.catalog.citation-revision"],
                "release_id": release_id,
                "release_dir": str(release_dir),
                "compose_file": str(compose_file),
                "compose_sha256": sha256_file(compose_file),
                "bundle_sha256": bundle_sha256,
                "support_images": support_images,
                "created_at": utc_now(),
                "migration_status": "unmigrated",
                "data_rebuild_status": "pending",
                "restore_status": "not_required" if self.layout.active.exists() else "pending",
            }
            atomic_json(self.layout.candidate, candidate)
            print(f"Candidate ready: {image}")
            print(f"Bundle: {revision} ({release_dir})")

    def candidate_state(self) -> dict[str, Any]:
        candidate = require_json(self.layout.candidate)
        if candidate["host_id"] != self.host().host_id:
            raise CatalogError("candidate belongs to another host")
        compose_file = Path(candidate["compose_file"])
        if not compose_file.is_file() or sha256_file(compose_file) != candidate["compose_sha256"]:
            raise CatalogError("candidate Compose file is missing or changed")
        release_dir = Path(candidate["release_dir"])
        if sha256_tree(release_dir, excluded={"docker-compose.yml"}) != candidate["bundle_sha256"]:
            raise CatalogError("candidate release bundle is missing or changed")
        return candidate

    def verify_release_artifact(self, release: dict[str, Any], name: str) -> Path:
        if release.get("host_id") != self.host().host_id:
            raise CatalogError(f"{name} release belongs to another host")
        compose_file = Path(release["compose_file"])
        if not compose_file.is_file() or sha256_file(compose_file) != release.get("compose_sha256"):
            raise CatalogError(f"{name} Compose artifact is missing or changed")
        release_dir = Path(release["release_dir"])
        if sha256_tree(release_dir, excluded={"docker-compose.yml"}) != release.get("bundle_sha256"):
            raise CatalogError(f"{name} release bundle is missing or changed")
        return compose_file

    def update_candidate(self, candidate: dict[str, Any], **values: Any) -> dict[str, Any]:
        candidate = dict(candidate)
        candidate.update(values)
        atomic_json(self.layout.candidate, candidate)
        return candidate

    def active_state(self) -> dict[str, Any] | None:
        return read_json(self.layout.active)

    def db_identity(self, compose_file: Path) -> tuple[str, str]:
        output = self.command_output(
            self.compose_command(
                compose_file,
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'printf "%s\\n%s\\n" "$POSTGRES_USER" "$POSTGRES_DB"',
            )
        )
        lines = output.splitlines()
        if len(lines) != 2:
            raise CatalogError("could not read database identity from the container")
        return validate_identifier(lines[0]), validate_identifier(lines[1])

    def psql(self, compose_file: Path, user: str, database: str, sql: str) -> str:
        return self.command_output(
            self.compose_command(
                compose_file,
                "exec",
                "-T",
                "db",
                "psql",
                "--username",
                user,
                "--dbname",
                database,
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                sql,
            )
        )

    def database_counts(self, compose_file: Path, user: str, database: str) -> dict[str, int]:
        exists = self.psql(
            compose_file,
            user,
            database,
            "SELECT to_regclass('public.citation_publication') IS NOT NULL;",
        )
        if exists != "t":
            return {"citation_publication": 0, "primary_publication": 0}
        output = self.psql(
            compose_file,
            user,
            database,
            "SELECT count(*), count(*) FILTER (WHERE is_primary) FROM citation_publication;",
        )
        total, primary = output.split("|", 1)
        return {"citation_publication": int(total), "primary_publication": int(primary)}

    def postgres_versions(self, compose_file: Path, user: str, database: str) -> dict[str, str]:
        return {
            "server": self.psql(compose_file, user, database, "SHOW server_version;"),
            "pg_dump": self.command_output(
                self.compose_command(compose_file, "exec", "-T", "db", "pg_dump", "--version")
            ),
            "pg_restore": self.command_output(
                self.compose_command(compose_file, "exec", "-T", "db", "pg_restore", "--version")
            ),
        }

    def backup(self, *, scheduled: bool = False) -> Path:
        with self.locked():
            active = require_json(self.layout.active)
            self.verify_release_artifact(active, "active")
            compose_file = self.active_compose()
            user, database = self.db_identity(compose_file)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = self.layout.backups / f"{database}-{timestamp}.dump"
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                with temporary.open("wb") as destination:
                    self.runner.to_file(
                        self.compose_command(
                            compose_file,
                            "exec",
                            "-T",
                            "db",
                            "pg_dump",
                            "--username",
                            user,
                            "--format",
                            "custom",
                            "--no-owner",
                            "--no-privileges",
                            database,
                        ),
                        destination,
                    )
                    destination.flush()
                    os.fsync(destination.fileno())
                with temporary.open("rb") as source:
                    self.runner.from_file(
                        self.compose_command(compose_file, "exec", "-T", "db", "pg_restore", "--list"),
                        source,
                    )
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
                fsync_directory(target.parent)
            finally:
                temporary.unlink(missing_ok=True)
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "kind": "backup",
                "host_id": self.host().host_id,
                "created_at": utc_now(),
                "artifact": str(target),
                "bytes": target.stat().st_size,
                "sha256": sha256_file(target),
                "database": database,
                "release_id": active["release_id"],
                "image": active["image"],
                "counts": self.database_counts(compose_file, user, database),
                "postgres": self.postgres_versions(compose_file, user, database),
                "scheduled": scheduled,
            }
            receipt_path = self.layout.receipts / "backups" / f"{target.name}.json"
            atomic_json(receipt_path, receipt)
            atomic_write(target.with_suffix(target.suffix + ".sha256"), f"{receipt['sha256']}  {target.name}\n".encode())
            self.prune_backups(days=30)
            print(f"Backup created: {target}")
            return target

    def prune_backups(self, *, days: int) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for receipt_path in (self.layout.receipts / "backups").glob("*.json"):
            receipt = read_json(receipt_path)
            if not receipt or parse_utc(receipt["created_at"]) >= cutoff:
                continue
            Path(receipt["artifact"]).unlink(missing_ok=True)
            Path(receipt["artifact"] + ".sha256").unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)

    @staticmethod
    def dump_kind(path: Path) -> str:
        if path.name.endswith(".sql.xz"):
            return "sql-xz"
        if path.suffix == ".sql":
            return "sql"
        if path.suffix == ".dump":
            return "custom"
        raise CatalogError("DUMP must end in .dump, .sql, or .sql.xz")

    @staticmethod
    def verify_dump_checksum(path: Path) -> None:
        checksum_path = Path(f"{path}.sha256")
        if not checksum_path.exists():
            return
        fields = checksum_path.read_text(encoding="utf-8").strip().split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
            raise CatalogError(f"invalid checksum file: {checksum_path}")
        recorded_name = fields[1].lstrip("*")
        if recorded_name != path.name:
            raise CatalogError(f"checksum file names another artifact: {recorded_name}")
        if sha256_file(path) != fields[0]:
            raise CatalogError(f"dump checksum does not match: {path}")

    @staticmethod
    def validate_plain_dump(path: Path, kind: str) -> None:
        opener = lzma.open if kind == "sql-xz" else open
        pattern = re.compile(r"^(?:CREATE|DROP|ALTER)\s+DATABASE\b|^\\connect\b", re.I)
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as source:
                for line in source:
                    if pattern.search(line.strip()):
                        raise CatalogError("plain SQL dump contains database-level commands")
        except lzma.LZMAError as error:
            raise CatalogError(f"invalid xz dump: {error}") from error

    def terminate_connections(self, compose_file: Path, user: str, database: str) -> None:
        self.psql(
            compose_file,
            user,
            "postgres",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{database}' AND pid <> pg_backend_pid();",
        )

    def restore(self, dump: Path, confirm: str) -> None:
        dump = dump.expanduser().resolve()
        if not dump.is_file() or dump.stat().st_size == 0:
            raise CatalogError(f"dump is missing or empty: {dump}")
        self.verify_dump_checksum(dump)
        kind = self.dump_kind(dump)
        if kind != "custom":
            self.validate_plain_dump(dump, kind)
        with self.locked():
            if self.layout.active.exists():
                raise CatalogError("this restore command is limited to a fresh host with no active release")
            candidate = self.candidate_state()
            if candidate.get("restore_status") != "pending":
                raise CatalogError(f"candidate restore status is {candidate.get('restore_status')}")
            compose_file = Path(candidate["compose_file"])
            self.compose(compose_file, "up", "-d", "--wait", "db", capture=False)
            user, database = self.db_identity(compose_file)
            if confirm != database:
                raise CatalogError(f"set CONFIRM={database} to authorize the database swap")
            candidate = self.update_candidate(
                candidate,
                restore_status="in_progress",
                restore_started_at=utc_now(),
                restore_artifact=str(dump),
                restore_sha256=sha256_file(dump),
            )
            suffix = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{os.getpid()}"
            temporary_database = validate_identifier(f"{database}_restore_{suffix}")
            old_database = validate_identifier(f"{database}_before_{suffix}")
            try:
                self.psql(
                    compose_file,
                    user,
                    "postgres",
                    f'CREATE DATABASE "{temporary_database}" OWNER "{user}";',
                )
                if kind == "custom":
                    with dump.open("rb") as source:
                        self.runner.from_file(
                            self.compose_command(
                                compose_file,
                                "exec",
                                "-T",
                                "db",
                                "pg_restore",
                                "--username",
                                user,
                                "--dbname",
                                temporary_database,
                                "--exit-on-error",
                                "--no-owner",
                                "--no-privileges",
                            ),
                            source,
                        )
                else:
                    command = self.compose_command(
                        compose_file,
                        "exec",
                        "-T",
                        "db",
                        "psql",
                        "--username",
                        user,
                        "--dbname",
                        temporary_database,
                        "--set",
                        "ON_ERROR_STOP=1",
                        "--quiet",
                    )
                    process = subprocess.Popen(command, stdin=subprocess.PIPE)
                    assert process.stdin is not None
                    opener = lzma.open if kind == "sql-xz" else open
                    with opener(dump, "rb") as source:
                        shutil.copyfileobj(source, process.stdin)
                    process.stdin.close()
                    if process.wait() != 0:
                        raise CatalogError("plain SQL restore failed")
                counts = self.database_counts(compose_file, user, temporary_database)
                if counts["citation_publication"] <= 0:
                    raise CatalogError("restored database contains no citation publications")
                invalid_constraints = self.psql(
                    compose_file,
                    user,
                    temporary_database,
                    "SELECT count(*) FROM pg_constraint WHERE NOT convalidated;",
                )
                if int(invalid_constraints) != 0:
                    raise CatalogError("restored database has unvalidated constraints")
                self.terminate_connections(compose_file, user, database)
                self.terminate_connections(compose_file, user, temporary_database)
                self.psql(
                    compose_file,
                    user,
                    "postgres",
                    f'ALTER DATABASE "{database}" RENAME TO "{old_database}";',
                )
                try:
                    self.psql(
                        compose_file,
                        user,
                        "postgres",
                        f'ALTER DATABASE "{temporary_database}" RENAME TO "{database}";',
                    )
                except Exception:
                    self.psql(
                        compose_file,
                        user,
                        "postgres",
                        f'ALTER DATABASE "{old_database}" RENAME TO "{database}";',
                    )
                    raise
            except Exception as error:
                with contextlib.suppress(Exception):
                    self.terminate_connections(compose_file, user, temporary_database)
                    self.psql(
                        compose_file,
                        user,
                        "postgres",
                        f'DROP DATABASE IF EXISTS "{temporary_database}";',
                    )
                self.update_candidate(
                    candidate,
                    restore_status="failed",
                    restore_failed_at=utc_now(),
                    restore_error=str(error),
                )
                raise
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "kind": "restore",
                "host_id": self.host().host_id,
                "created_at": utc_now(),
                "artifact": str(dump),
                "bytes": dump.stat().st_size,
                "sha256": sha256_file(dump),
                "database": database,
                "retained_database": old_database,
                "counts": counts,
                "postgres": self.postgres_versions(compose_file, user, database),
                "release_id": candidate["release_id"],
            }
            receipt_path = self.layout.receipts / "restores" / f"restore-{candidate['release_id']}.json"
            atomic_json(receipt_path, receipt)
            self.update_candidate(
                candidate,
                restore_status="succeeded",
                restore_receipt=str(receipt_path),
            )
            print(f"Restore complete: {database} contains {counts['citation_publication']} publications")

    def latest_valid_backup(self, active: dict[str, Any]) -> dict[str, Any]:
        receipts = sorted((self.layout.receipts / "backups").glob("*.json"), reverse=True)
        for path in receipts:
            receipt = read_json(path)
            if not receipt or receipt.get("release_id") != active.get("release_id"):
                continue
            if datetime.now(timezone.utc) - parse_utc(receipt["created_at"]) > timedelta(hours=24):
                continue
            artifact = Path(receipt["artifact"])
            if artifact.is_file() and sha256_file(artifact) == receipt["sha256"]:
                return receipt
        raise CatalogError("schema migration requires a verified backup from the active release under 24 hours old")

    def manage(self, compose_file: Path, *arguments: str, capture: bool = False) -> subprocess.CompletedProcess:
        return self.compose(
            compose_file,
            "run",
            "--rm",
            "--no-deps",
            "django",
            "python3",
            "manage.py",
            *arguments,
            capture=capture,
        )

    def schema_migrate(self, confirmed: bool) -> None:
        if not confirmed:
            raise CatalogError("set CONFIRM_SCHEMA_MIGRATION=1 to run schema-migrate")
        with self.locked():
            candidate = self.candidate_state()
            if candidate["migration_status"] != "unmigrated":
                raise CatalogError(f"candidate migration status is {candidate['migration_status']}")
            active = self.active_state()
            if active:
                self.verify_release_artifact(active, "active")
            if active:
                self.latest_valid_backup(active)
            elif candidate.get("restore_status") != "succeeded":
                raise CatalogError("fresh-host migration requires a successful restore receipt")
            compose_file = Path(candidate["compose_file"])
            self.compose(compose_file, "up", "-d", "--wait", "db", "redis", "elasticsearch", capture=False)
            if active:
                active_compose = Path(active["compose_file"])
                self.compose(active_compose, "stop", "django", "scheduler", capture=False, check=False)
            candidate = self.update_candidate(
                candidate,
                migration_status="in_progress",
                migration_started_at=utc_now(),
            )
            try:
                self.manage(compose_file, "makemigrations", "--check", "--dry-run")
                plan = self.manage(compose_file, "migrate", "--plan", capture=True).stdout or ""
                self.manage(compose_file, "migrate", "--noinput")
                self.manage(compose_file, "migrate", "--check")
            except Exception as error:
                self.update_candidate(
                    candidate,
                    migration_status="failed",
                    migration_failed_at=utc_now(),
                    migration_error=str(error),
                )
                raise
            self.update_candidate(
                candidate,
                migration_status="succeeded",
                migration_completed_at=utc_now(),
                migration_plan=plan.splitlines(),
            )
            print("Schema migration completed; the application remains in maintenance until deploy")

    def alias_targets(self, compose_file: Path) -> dict[str, str]:
        result = self.compose(
            compose_file,
            "exec",
            "-T",
            "elasticsearch",
            "curl",
            "-fsS",
            "http://localhost:9200/_alias",
        )
        payload = json.loads(result.stdout or "{}")
        targets: dict[str, str] = {}
        for index_name, index_data in payload.items():
            for alias in index_data.get("aliases", {}):
                if alias in MANAGED_ALIASES or alias.startswith("autocomplete_"):
                    targets[alias] = index_name
        return targets

    def set_alias_targets(self, compose_file: Path, targets: dict[str, str]) -> None:
        current = self.alias_targets(compose_file)
        actions: list[dict[str, dict[str, str]]] = []
        for alias, index_name in current.items():
            if alias in targets or alias in MANAGED_ALIASES or alias.startswith("autocomplete_"):
                actions.append({"remove": {"index": index_name, "alias": alias}})
        for alias, index_name in targets.items():
            actions.append({"add": {"index": index_name, "alias": alias}})
        if not actions:
            return
        body = json.dumps({"actions": actions})
        self.compose(
            compose_file,
            "exec",
            "-T",
            "elasticsearch",
            "curl",
            "-fsS",
            "-XPOST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            body,
            "http://localhost:9200/_aliases",
        )

    def data_rebuild(self) -> None:
        with self.locked():
            candidate = self.candidate_state()
            if candidate["migration_status"] != "succeeded":
                raise CatalogError("schema-migrate must succeed before data-rebuild")
            if candidate["data_rebuild_status"] != "pending":
                raise CatalogError(f"candidate rebuild status is {candidate['data_rebuild_status']}")
            compose_file = Path(candidate["compose_file"])
            self.compose(compose_file, "up", "-d", "--wait", "db", "redis", "elasticsearch", capture=False)
            before = self.alias_targets(compose_file)
            candidate = self.update_candidate(
                candidate,
                data_rebuild_status="in_progress",
                search_aliases_before=before,
                data_rebuild_started_at=utc_now(),
            )
            try:
                self.manage(compose_file, "rebuild_es_index")
                self.manage(compose_file, "validate_search_indexes")
                self.manage(compose_file, "populate_visualization_cache", "--clear")
                after = self.alias_targets(compose_file)
                if not after:
                    raise CatalogError("search rebuild produced no managed aliases")
            except Exception as error:
                with contextlib.suppress(Exception):
                    self.set_alias_targets(compose_file, before)
                self.update_candidate(
                    candidate,
                    data_rebuild_status="failed",
                    data_rebuild_failed_at=utc_now(),
                    data_rebuild_error=str(error),
                )
                raise
            self.update_candidate(
                candidate,
                data_rebuild_status="succeeded",
                data_rebuild_completed_at=utc_now(),
                search_aliases_after=after,
            )
            print("Search indexes and visualization cache rebuilt and validated")

    def candidate_retry(self, confirmed: bool) -> None:
        if not confirmed:
            raise CatalogError("set CONFIRM_CANDIDATE_RETRY=1 after investigating the failed operation")
        with self.locked():
            if self.layout.journal.exists():
                raise CatalogError("an unfinished transaction journal exists; run make recover")
            candidate = self.candidate_state()
            migration_status = candidate["migration_status"]
            rebuild_status = candidate["data_rebuild_status"]
            restore_status = candidate.get("restore_status")
            if restore_status in {"failed", "in_progress"}:
                next_values = {"restore_status": "pending"}
                stage = "restore"
            elif migration_status in {"failed", "in_progress"}:
                next_values = {
                    "migration_status": "unmigrated",
                    "data_rebuild_status": "pending",
                }
                stage = "schema-migrate"
            elif rebuild_status in {"failed", "in_progress"}:
                aliases_before = candidate.get("search_aliases_before")
                if not isinstance(aliases_before, dict):
                    raise CatalogError("candidate has no pre-rebuild search alias snapshot")
                compose_file = Path(candidate["compose_file"])
                self.set_alias_targets(compose_file, aliases_before)
                if self.alias_targets(compose_file) != aliases_before:
                    raise CatalogError("could not restore the pre-rebuild search aliases")
                next_values = {"data_rebuild_status": "pending"}
                stage = "data-rebuild"
            else:
                raise CatalogError("candidate has no failed or interrupted stage to retry")
            for key in list(candidate):
                if (
                    key.startswith("migration_failed_")
                    or key.startswith("data_rebuild_failed_")
                    or key.startswith("restore_failed_")
                    or key in {"migration_error", "data_rebuild_error", "restore_error"}
                ):
                    candidate.pop(key)
            candidate = self.update_candidate(
                candidate,
                **next_values,
                incident_reset_at=utc_now(),
                incident_reset_by=getpass.getuser(),
            )
            append_json_line(
                self.layout.history,
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "candidate-retry",
                    "result": "authorized",
                    "host_id": self.host().host_id,
                    "release_id": candidate["release_id"],
                    "stage": stage,
                    "timestamp": utc_now(),
                },
            )
            print(f"Candidate unlocked for {stage}; rerun that command")

    def write_journal(self, value: dict[str, Any]) -> None:
        value = dict(value)
        value["schema_version"] = SCHEMA_VERSION
        atomic_json(self.layout.journal, value)

    def update_journal(self, journal: dict[str, Any], checkpoint: str, **values: Any) -> dict[str, Any]:
        journal = dict(journal)
        journal.update(values)
        journal["checkpoint"] = checkpoint
        journal["updated_at"] = utc_now()
        self.write_journal(journal)
        return journal

    def append_history_once(self, record: dict[str, Any]) -> None:
        operation_id = record["operation_id"]
        result = record["result"]
        if self.layout.history.exists():
            try:
                with self.layout.history.open(encoding="utf-8") as source:
                    for line in source:
                        existing = json.loads(line)
                        if (
                            existing.get("operation_id") == operation_id
                            and existing.get("result") == result
                        ):
                            return
            except (OSError, json.JSONDecodeError) as error:
                raise CatalogError(f"invalid release history {self.layout.history}: {error}") from error
        append_json_line(self.layout.history, record)

    def restore_release_state(
        self,
        active: dict[str, Any] | None,
        rollback: dict[str, Any] | None,
    ) -> None:
        if active:
            atomic_json(self.layout.active, active)
        else:
            self.layout.active.unlink(missing_ok=True)
        if rollback:
            atomic_json(self.layout.rollback, rollback)
        else:
            self.layout.rollback.unlink(missing_ok=True)

    def finalize_committed(self, journal: dict[str, Any]) -> None:
        active = journal.get("new_active")
        if not active:
            raise CatalogError("committed journal has no new active release")
        compose_file = self.verify_release_artifact(active, "committed")
        self.set_alias_targets(compose_file, active.get("search_aliases", {}))
        self.compose(
            compose_file,
            "up",
            "-d",
            "--no-build",
            "--wait",
            "--remove-orphans",
            capture=False,
        )
        self.verify_runtime(compose_file)
        self.publish_compose(compose_file)
        self.restore_release_state(active, journal.get("new_rollback"))
        self.append_history_once(
            {
                "schema_version": SCHEMA_VERSION,
                "operation": journal["operation"],
                "operation_id": journal["operation_id"],
                "result": "succeeded",
                "host_id": self.host().host_id,
                "release_id": active["release_id"],
                "image": active["image"],
                "timestamp": utc_now(),
            }
        )
        if journal["operation"] == "deploy":
            self.layout.candidate.unlink(missing_ok=True)
        self.layout.journal.unlink(missing_ok=True)

    def verify_runtime(self, compose_file: Path) -> None:
        self.compose(compose_file, "exec", "-T", "nginx", "nginx", "-t")
        domain = self.host().domain
        self.compose(
            compose_file,
            "exec",
            "-T",
            "django",
            "curl",
            "-fsS",
            "-H",
            f"Host: {domain}",
            "http://nginx/",
        )

    def publish_compose(self, source: Path) -> None:
        atomic_write(self.layout.canonical_compose, source.read_bytes(), 0o600)

    def restore_runtime(self, active: dict[str, Any] | None, candidate_compose: Path) -> bool:
        try:
            if active:
                active_compose = self.verify_release_artifact(active, "active")
                self.set_alias_targets(active_compose, active.get("search_aliases", {}))
                self.compose(active_compose, "up", "-d", "--no-build", "--wait", "--remove-orphans", capture=False)
                self.verify_runtime(active_compose)
                self.publish_compose(active_compose)
            else:
                self.compose(candidate_compose, "rm", "-s", "-f", capture=False, check=False)
            return True
        except Exception as error:
            print(f"ERROR: runtime recovery failed: {error}", file=sys.stderr)
            return False

    def deploy(self) -> None:
        with self.locked():
            if self.layout.journal.exists():
                raise CatalogError("an unfinished transaction journal exists; run make recover")
            candidate = self.candidate_state()
            if candidate["migration_status"] != "succeeded":
                raise CatalogError("schema-migrate must succeed before deploy")
            if candidate["data_rebuild_status"] != "succeeded":
                raise CatalogError("data-rebuild must succeed before deploy")
            compose_file = Path(candidate["compose_file"])
            self.manage(compose_file, "migrate", "--check")
            self.manage(compose_file, "validate_search_indexes")
            expected_aliases = candidate.get("search_aliases_after")
            if not isinstance(expected_aliases, dict) or not expected_aliases:
                raise CatalogError("candidate has no validated search alias snapshot")
            if self.alias_targets(compose_file) != expected_aliases:
                raise CatalogError("live search aliases differ from the validated candidate")
            active = self.active_state()
            if active:
                self.verify_release_artifact(active, "active")
            rollback = read_json(self.layout.rollback)
            journal = {
                "operation": "deploy",
                "operation_id": candidate["operation_id"],
                "started_at": utc_now(),
                "checkpoint": "candidate-started",
                "candidate": candidate,
                "previous_active": active,
                "previous_rollback": rollback,
            }
            self.write_journal(journal)
            try:
                self.compose(
                    compose_file,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--remove-orphans",
                    capture=False,
                )
                self.verify_runtime(compose_file)
                journal = self.update_journal(journal, "runtime-ready")
                self.publish_compose(compose_file)
                journal = self.update_journal(journal, "root-published")
                new_active = {
                    "schema_version": SCHEMA_VERSION,
                    "host_id": self.host().host_id,
                    "release_id": candidate["release_id"],
                    "image": candidate["image"],
                    "bundle_revision": candidate["bundle_revision"],
                    "citation_revision": candidate["citation_revision"],
                    "compose_file": candidate["compose_file"],
                    "compose_sha256": candidate["compose_sha256"],
                    "release_dir": candidate["release_dir"],
                    "bundle_sha256": candidate["bundle_sha256"],
                    "support_images": candidate["support_images"],
                    "search_aliases": candidate.get("search_aliases_after", {}),
                    "deployed_at": utc_now(),
                    "operator": getpass.getuser(),
                }
                if active:
                    atomic_json(self.layout.rollback, active)
                else:
                    self.layout.rollback.unlink(missing_ok=True)
                atomic_json(self.layout.active, new_active)
                journal = self.update_journal(
                    journal,
                    "state-published",
                    new_active=new_active,
                    new_rollback=active,
                )
                journal = self.update_journal(journal, "committed")
                self.append_history_once(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "deploy",
                        "operation_id": candidate["operation_id"],
                        "result": "succeeded",
                        "host_id": self.host().host_id,
                        "release_id": new_active["release_id"],
                        "image": new_active["image"],
                        "timestamp": utc_now(),
                    },
                )
                self.layout.candidate.unlink(missing_ok=True)
                self.layout.journal.unlink(missing_ok=True)
            except Exception as error:
                persisted_journal = read_json(self.layout.journal) or journal
                if persisted_journal.get("checkpoint") == "committed":
                    raise CatalogError(
                        f"deployment committed but finalization failed; run make recover: {error}"
                    ) from error
                recovered = self.restore_runtime(active, compose_file)
                if recovered:
                    try:
                        self.restore_release_state(active, rollback)
                    except Exception as state_error:
                        recovered = False
                        error = CatalogError(f"{error}; prior state recovery failed: {state_error}")
                self.update_journal(
                    journal,
                    "recovered" if recovered else "unrecovered",
                    error=str(error),
                )
                self.append_history_once(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "deploy",
                        "operation_id": candidate["operation_id"],
                        "result": "failed-recovered" if recovered else "failed-unrecovered",
                        "host_id": self.host().host_id,
                        "timestamp": utc_now(),
                        "error": str(error),
                    },
                )
                if recovered:
                    self.layout.journal.unlink(missing_ok=True)
                raise CatalogError(f"deployment failed; prior runtime recovered={recovered}: {error}") from error
            print(f"Deployed {candidate['image']} on {self.host().host_id}")

    def rollback_release(self) -> None:
        with self.locked():
            if self.layout.journal.exists():
                raise CatalogError("an unfinished transaction journal exists; run make recover")
            active = require_json(self.layout.active)
            rollback = require_json(self.layout.rollback)
            self.verify_release_artifact(active, "active")
            compose_file = self.verify_release_artifact(rollback, "rollback")
            journal = {
                "operation": "rollback",
                "operation_id": uuid.uuid4().hex,
                "started_at": utc_now(),
                "checkpoint": "candidate-started",
                "previous_active": active,
                "previous_rollback": rollback,
            }
            self.write_journal(journal)
            try:
                self.set_alias_targets(compose_file, rollback.get("search_aliases", {}))
                self.compose(
                    compose_file,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--remove-orphans",
                    capture=False,
                )
                self.verify_runtime(compose_file)
                journal = self.update_journal(journal, "runtime-ready")
                self.publish_compose(compose_file)
                journal = self.update_journal(journal, "root-published")
                rolled_back = dict(rollback)
                rolled_back.update(deployed_at=utc_now(), operator=getpass.getuser())
                atomic_json(self.layout.active, rolled_back)
                atomic_json(self.layout.rollback, active)
                journal = self.update_journal(
                    journal,
                    "state-published",
                    new_active=rolled_back,
                    new_rollback=active,
                )
                journal = self.update_journal(journal, "committed")
                self.append_history_once(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "rollback",
                        "operation_id": journal["operation_id"],
                        "result": "succeeded",
                        "host_id": self.host().host_id,
                        "release_id": rolled_back["release_id"],
                        "image": rolled_back["image"],
                        "timestamp": utc_now(),
                    },
                )
                self.layout.journal.unlink(missing_ok=True)
            except Exception as error:
                persisted_journal = read_json(self.layout.journal) or journal
                if persisted_journal.get("checkpoint") == "committed":
                    raise CatalogError(
                        f"rollback committed but finalization failed; run make recover: {error}"
                    ) from error
                recovered = self.restore_runtime(active, compose_file)
                if recovered:
                    try:
                        self.restore_release_state(active, rollback)
                    except Exception as state_error:
                        recovered = False
                        error = CatalogError(f"{error}; prior state recovery failed: {state_error}")
                self.update_journal(
                    journal,
                    "recovered" if recovered else "unrecovered",
                    error=str(error),
                )
                self.append_history_once(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "rollback",
                        "operation_id": journal["operation_id"],
                        "result": "failed-recovered" if recovered else "failed-unrecovered",
                        "host_id": self.host().host_id,
                        "timestamp": utc_now(),
                        "error": str(error),
                    },
                )
                if recovered:
                    self.layout.journal.unlink(missing_ok=True)
                raise CatalogError(f"rollback failed; active runtime recovered={recovered}: {error}") from error
            print(f"Rolled back to {rollback['image']}")

    def recover(self) -> None:
        with self.locked():
            journal = require_json(self.layout.journal)
            if journal.get("checkpoint") == "committed":
                self.finalize_committed(journal)
                print("Finalized the committed release and repaired its history")
                return
            active = journal.get("previous_active")
            candidate_data = journal.get("candidate") or journal.get("previous_rollback")
            if not candidate_data:
                raise CatalogError("journal has no runtime artifact for recovery")
            candidate_compose = self.verify_release_artifact(candidate_data, "recovery")
            if not self.restore_runtime(active, candidate_compose):
                self.update_journal(journal, "unrecovered")
                raise CatalogError("automatic recovery failed; journal retained")
            previous_rollback = journal.get("previous_rollback")
            self.restore_release_state(active, previous_rollback)
            self.append_history_once(
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "recover",
                    "operation_id": journal["operation_id"],
                    "result": "succeeded",
                    "host_id": self.host().host_id,
                    "timestamp": utc_now(),
                    "recovered_operation": journal["operation"],
                },
            )
            self.layout.journal.unlink(missing_ok=True)
            print("Recovered the previous active runtime and state")

    def active_compose(self) -> Path:
        active = require_json(self.layout.active)
        self.verify_release_artifact(active, "active")
        if not self.layout.canonical_compose.is_file():
            raise CatalogError("canonical active Compose file is missing")
        if sha256_file(self.layout.canonical_compose) != active["compose_sha256"]:
            raise CatalogError("canonical active Compose file does not match active state")
        return self.layout.canonical_compose

    def start(self) -> None:
        with self.locked():
            self.compose(self.active_compose(), "up", "-d", "--no-build", "--wait", capture=False)

    def stop(self) -> None:
        with self.locked():
            self.compose(self.active_compose(), "stop", capture=False)

    def logs(self) -> None:
        self.compose(self.active_compose(), "logs", "-f", capture=False)

    def status(self) -> None:
        host = self.host()
        active = self.active_state()
        rollback = read_json(self.layout.rollback)
        candidate = read_json(self.layout.candidate)
        print(f"Host: {host.host_id} domain={host.domain} project={host.project_name}")
        if active:
            print(f"Active: {active['image']} bundle={active['bundle_revision']}")
        else:
            print("Active: none")
        print(f"Rollback: {rollback['image'] if rollback else 'none'}")
        print(f"Candidate: {candidate['image'] if candidate else 'none'}")
        if self.layout.journal.exists():
            journal = require_json(self.layout.journal)
            print(f"Journal: {journal['operation']} checkpoint={journal['checkpoint']}")
        result = self.runner.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={host.project_name}",
                "--format",
                "table {{.Names}}\t{{.Image}}\t{{.Status}}",
            ],
            check=False,
        )
        if result.stdout:
            print(result.stdout.rstrip())

    def release_report(self) -> Path:
        with self.locked():
            host = self.host()
            active = require_json(self.layout.active)
            compose_file = self.active_compose()
            user, database = self.db_identity(compose_file)
            settings_output = self.command_output(
                self.compose_command(
                    compose_file,
                    "run",
                    "--rm",
                    "--no-deps",
                    "django",
                    "python3",
                    "-c",
                    "import django; django.setup(); "
                    "from django.conf import settings; import psycopg2; "
                    "print(settings.EMAIL_BACKEND); print(psycopg2.__version__)",
                )
            ).splitlines()
            search_validation = (
                self.manage(compose_file, "validate_search_indexes", capture=True).stdout or ""
            ).strip()
            images = self.command_output(self.compose_command(compose_file, "config", "--images")).splitlines()
            latest_backup = None
            with contextlib.suppress(CatalogError):
                latest_backup = self.latest_valid_backup(active)
            report = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_now(),
                "host_id": host.host_id,
                "domain": host.domain,
                "active": active,
                "rollback": read_json(self.layout.rollback),
                "candidate": read_json(self.layout.candidate),
                "service_images": images,
                "postgres": self.postgres_versions(compose_file, user, database),
                "database_counts": self.database_counts(compose_file, user, database),
                "email_backend": settings_output[0] if settings_output else "unknown",
                "psycopg2": settings_output[1] if len(settings_output) > 1 else "unknown",
                "search_aliases": self.alias_targets(compose_file),
                "search_validation": search_validation,
                "latest_backup": latest_backup,
                "warnings": ["Backups are on-host only; host or attached-volume loss is unrecoverable."],
            }
            path = self.layout.reports / f"release-{active['release_id']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
            atomic_json(path, report)
            print(json.dumps(report, indent=2, sort_keys=True))
            print(f"Release report written: {path}")
            return path


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    provision = subparsers.add_parser("host-provision")
    provision.add_argument("--host-id", required=True, choices=sorted(HOST_IDS))
    provision.add_argument("--operator", default=os.environ.get("SUDO_USER") or getpass.getuser())
    subparsers.add_parser("host-check")
    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("--image", required=True)
    candidate.add_argument("--bundle-revision", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--scheduled", action="store_true")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--dump", required=True, type=Path)
    restore.add_argument("--confirm", required=True)
    migrate = subparsers.add_parser("schema-migrate")
    migrate.add_argument("--confirmed", action="store_true")
    for command in (
        "data-rebuild",
        "deploy",
        "rollback",
        "recover",
        "status",
        "release-report",
        "start",
        "stop",
        "logs",
    ):
        subparsers.add_parser(command)
    retry = subparsers.add_parser("candidate-retry")
    retry.add_argument("--confirmed", action="store_true")
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    controller = Controller(repository_root(), Layout.from_environment())
    try:
        if args.command == "host-provision":
            controller.host_provision(args.host_id, args.operator)
        elif args.command == "host-check":
            controller.host_check()
        elif args.command == "candidate":
            controller.candidate_create(args.image, args.bundle_revision)
        elif args.command == "backup":
            controller.backup(scheduled=args.scheduled)
        elif args.command == "restore":
            controller.restore(args.dump, args.confirm)
        elif args.command == "schema-migrate":
            controller.schema_migrate(args.confirmed)
        elif args.command == "data-rebuild":
            controller.data_rebuild()
        elif args.command == "candidate-retry":
            controller.candidate_retry(args.confirmed)
        elif args.command == "deploy":
            controller.deploy()
        elif args.command == "rollback":
            controller.rollback_release()
        elif args.command == "recover":
            controller.recover()
        elif args.command == "status":
            controller.status()
        elif args.command == "release-report":
            controller.release_report()
        elif args.command == "start":
            controller.start()
        elif args.command == "stop":
            controller.stop()
        elif args.command == "logs":
            controller.logs()
    except CatalogError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"ERROR: command failed with exit code {error.returncode}: {' '.join(error.cmd)}", file=sys.stderr)
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
