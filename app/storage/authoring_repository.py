from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
import time
from typing import Any, Iterator
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.tenant_access import tenant_directory_key
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    INSTITUTION_PROFILE_ID_MAX_LENGTH,
    INSTITUTION_PROFILE_ID_PATTERN,
    AuthoringEventType,
    AuthoringProject,
    AuthoringProjectStatus,
)
from app.schemas.authoring_integrity import semantic_content_hash


_AUTHORING_THREAD_LOCK = RLock()
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0
_REPLACE_RETRY_SECONDS = 2.0
_REPLACE_RETRY_INTERVAL_SECONDS = 0.05
_EVENT_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_ALLOWED_EVENT_METADATA_KEYS = frozenset(
    {
        "export_format",
        "file_sha256",
        "from_status",
        "lint_error_count",
        "lint_warning_count",
        "migration",
        "reason_sha256",
        "reason_supplied",
        "self_freeze",
        "template_id_value",
        "to_status",
        "training_only",
    }
)
_SENSITIVE_EVENT_KEY_PARTS = frozenset(
    {
        "body",
        "clause",
        "content",
        "definition",
        "draft",
        "feedback",
        "message",
        "note",
        "purpose",
        "reference",
        "scope",
        "text",
        "title",
    }
)
_LEGACY_SENSITIVE_EVENT_KEY_PARTS = frozenset(
    {
        "body",
        "clause",
        "content",
        "definition",
        "draft",
        "note",
        "purpose",
        "reference",
        "scope",
        "text",
        "title",
    }
)


class AuthoringRepositoryError(RuntimeError):
    """Base error for the isolated authoring repository."""


class AuthoringProjectAlreadyExistsError(AuthoringRepositoryError):
    """Raised when a caller tries to create an existing project UUID."""


class AuthoringProjectNotFoundError(KeyError, AuthoringRepositoryError):
    """Raised for both missing and cross-tenant projects to avoid disclosure."""


class AuthoringRevisionConflictError(AuthoringRepositoryError):
    """Raised when the caller's optimistic concurrency token is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Authoring project revision conflict "
            f"(expected={expected_revision}, actual={actual_revision})."
        )


class AuthoringRepositoryIntegrityError(AuthoringRepositoryError):
    """Raised when committed authoring storage fails structural validation."""


@dataclass
class AuthoringProfilePurgeResult:
    """Resumable result for deleting one institution's isolated drafts."""

    profile_id: str
    tenant_id: str
    requested_project_count: int = 0
    deleted_project_count: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return (
            not self.failures
            and self.deleted_project_count == self.requested_project_count
        )


class AuthoringRepository:
    """Physically isolated JSON storage for regulation-authoring projects.

    A commit writes an immutable snapshot and an immutable, content-free event
    before atomically replacing a small project manifest. The manifest is the
    sole visibility boundary: a failed write can leave an unreferenced file,
    but it cannot expose a half-committed project or event to readers.
    """

    def __init__(self, settings_or_data_dir: Settings | Path):
        if isinstance(settings_or_data_dir, Settings):
            data_dir = settings_or_data_dir.data_dir
            root = settings_or_data_dir.authoring_dir
        else:
            data_dir = Path(settings_or_data_dir)
            root = data_dir / "authoring"
        self.data_dir = Path(data_dir)
        self.root = Path(root)
        self.projects_root = self.root / "projects"
        self.snapshots_root = self.root / "snapshots"
        self.events_root = self.root / "events"
        self.exports_root = self.root / "exports"
        # A content-free intent is durable before the first sensitive snapshot.
        # It lets startup recovery and institution purge find a project whose
        # initial manifest was never published because the process stopped.
        self.staging_root = self.root / ".staging"
        # A small content-free tombstone keeps a partially completed purge
        # discoverable and retryable after the active manifest is removed.
        self.purges_root = self.root / ".purges"
        self._initialize_storage()
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            self._recover_uncommitted_storage_unlocked()

    def create_project(
        self,
        project: AuthoringProject,
        *,
        actor: str,
        event_type: str = "created",
        reason: str | None = None,
        event_metadata: Mapping[str, object] | None = None,
    ) -> AuthoringProject:
        payload = _project_payload(project)
        project_id = _canonical_project_id(payload.get("project_id"))
        tenant_id = _tenant_id(payload.get("tenant_id"))
        profile_id = _profile_id(payload.get("profile_id"))
        actor_id = _actor_id(actor)
        event_name = _event_type(event_type)
        event_reason = _event_reason(reason)
        metadata = _safe_event_metadata(event_metadata)

        with _AUTHORING_THREAD_LOCK, self._write_lock():
            manifest_path = self._manifest_path(project_id)
            self._require_not_purging_unlocked(project_id)
            if manifest_path.is_file():
                raise AuthoringProjectAlreadyExistsError(
                    "An authoring project with this UUID already exists."
                )
            intent_path = self._staging_intent_path(project_id)
            if intent_path.exists():
                self._recover_staging_intent_unlocked(intent_path)
            intent = {
                "staging_schema_version": 1,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "created_at": _utc_now().isoformat(),
            }
            self._atomic_write_json(intent_path, intent)
            payload["project_id"] = project_id
            payload["tenant_id"] = tenant_id
            payload["profile_id"] = profile_id
            payload["revision"] = 1
            created = self._commit_generation(
                payload,
                previous_manifest=None,
                actor=actor_id,
                event_type=event_name,
                reason=event_reason,
                event_metadata=metadata,
            )
            # The manifest is now the durable visibility and ownership marker.
            # A failed unlink is harmless: startup/purge validates ownership and
            # removes the stale content-free intent without touching the commit.
            try:
                intent_path.unlink(missing_ok=True)
            except OSError:
                pass
            return created

    def save_project(
        self,
        project: AuthoringProject,
        *,
        tenant_id: str,
        expected_revision: int,
        actor: str,
        event_type: str = "updated",
        reason: str | None = None,
        event_metadata: Mapping[str, object] | None = None,
    ) -> AuthoringProject:
        payload = _project_payload(project)
        project_id = _canonical_project_id(payload.get("project_id"))
        requester_tenant_id = _tenant_id(tenant_id)
        project_tenant_id = _tenant_id(payload.get("tenant_id"))
        project_profile_id = _profile_id(payload.get("profile_id"))
        if project_tenant_id != requester_tenant_id:
            raise AuthoringProjectNotFoundError("Authoring project not found.")
        expected = _revision(expected_revision, allow_zero=True)
        actor_id = _actor_id(actor)
        event_name = _event_type(event_type)
        event_reason = _event_reason(reason)
        metadata = _safe_event_metadata(event_metadata)

        with _AUTHORING_THREAD_LOCK, self._write_lock():
            self._require_not_purging_unlocked(project_id)
            manifest = self._load_manifest(project_id)
            self._require_tenant(manifest, requester_tenant_id)
            actual = _revision(manifest.get("revision"))
            if expected != actual:
                raise AuthoringRevisionConflictError(
                    expected_revision=expected,
                    actual_revision=actual,
                )
            payload["project_id"] = project_id
            payload["tenant_id"] = requester_tenant_id
            payload["profile_id"] = project_profile_id
            payload["revision"] = actual + 1
            return self._commit_generation(
                payload,
                previous_manifest=manifest,
                actor=actor_id,
                event_type=event_name,
                reason=event_reason,
                event_metadata=metadata,
            )

    def save_exported_project(
        self,
        project: AuthoringProject,
        *,
        tenant_id: str,
        expected_revision: int,
        actor: str,
        frozen_revision: int,
        frozen_content_hash: str,
        export_format: str,
        content: bytes,
        content_sha256: str,
        event_metadata: Mapping[str, object],
    ) -> tuple[AuthoringProject, bytes]:
        """Atomically store one export and publish its EXPORTED generation.

        The artifact write and manifest transition deliberately share the same
        repository lock as profile purge. A purge that wins the lock removes the
        manifest before this method can write; an export that wins publishes the
        event before purge can perform its final directory sweep.
        """

        payload = _project_payload(project)
        project_id = _canonical_project_id(payload.get("project_id"))
        requester_tenant_id = _tenant_id(tenant_id)
        project_tenant_id = _tenant_id(payload.get("tenant_id"))
        project_profile_id = _profile_id(payload.get("profile_id"))
        if project_tenant_id != requester_tenant_id:
            raise AuthoringProjectNotFoundError("Authoring project not found.")
        expected = _revision(expected_revision, allow_zero=True)
        actor_id = _actor_id(actor)
        revision = _revision(frozen_revision)
        content_hash = _content_hash(frozen_content_hash)
        if export_format not in {"json", "markdown"}:
            raise ValueError("export_format must be json or markdown.")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes.")
        declared_file_hash = _content_hash(content_sha256)
        if hashlib.sha256(content).hexdigest() != declared_file_hash:
            raise AuthoringRepositoryIntegrityError(
                "Authoring export content does not match its declared SHA-256."
            )
        metadata = _safe_event_metadata(event_metadata)

        with _AUTHORING_THREAD_LOCK, self._write_lock():
            self._require_not_purging_unlocked(project_id)
            manifest = self._load_manifest(project_id)
            self._require_tenant(manifest, requester_tenant_id)
            self._require_profile(manifest, project_profile_id)
            actual = _revision(manifest.get("revision"))
            if expected != actual:
                raise AuthoringRevisionConflictError(
                    expected_revision=expected,
                    actual_revision=actual,
                )
            current = AuthoringProject.model_validate(
                self._load_current_snapshot(manifest)
            )
            if (
                current.status != AuthoringProjectStatus.CONTENT_FROZEN
                or current.frozen_revision != revision
                or current.frozen_content_hash != content_hash
                or current.semantic_content_hash != content_hash
                or semantic_content_hash(current) != content_hash
                or project.status != AuthoringProjectStatus.EXPORTED
                or project.frozen_revision != revision
                or project.frozen_content_hash != content_hash
                or project.semantic_content_hash != content_hash
                or semantic_content_hash(project) != content_hash
            ):
                raise AuthoringRepositoryIntegrityError(
                    "Authoring export transition does not match the committed frozen project."
                )
            payload["project_id"] = project_id
            payload["tenant_id"] = requester_tenant_id
            payload["profile_id"] = project_profile_id
            payload["revision"] = actual + 1
            suffix = "json" if export_format == "json" else "md"
            export_path = self._export_artifact_path_unlocked(
                project_id,
                frozen_revision=revision,
                content_hash=content_hash,
                suffix=suffix,
            )
            created = False
            try:
                created, stored = self._write_export_artifact_unlocked(
                    export_path,
                    content,
                )
                saved = self._commit_generation(
                    payload,
                    previous_manifest=manifest,
                    actor=actor_id,
                    event_type=AuthoringEventType.EXPORTED.value,
                    reason=None,
                    event_metadata=metadata,
                )
            except BaseException:
                if created:
                    export_path.unlink(missing_ok=True)
                raise
            return saved, stored

    def get_project(
        self,
        project_id: str,
        *,
        tenant_id: str,
        profile_id: str | None = None,
    ) -> AuthoringProject:
        canonical_id = _canonical_project_id(project_id)
        requester_tenant_id = _tenant_id(tenant_id)
        requester_profile_id = _profile_id(profile_id) if profile_id is not None else None
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            manifest = self._load_manifest(canonical_id)
            self._require_tenant(manifest, requester_tenant_id)
            if requester_profile_id is not None:
                self._require_profile(manifest, requester_profile_id)
            payload = self._load_current_snapshot(manifest)
            return AuthoringProject.model_validate(payload)

    def get_project_revision(
        self,
        project_id: str,
        *,
        tenant_id: str,
        profile_id: str,
        revision: int,
    ) -> AuthoringProject:
        """Load one committed immutable revision after scope and chain checks."""

        canonical_id = _canonical_project_id(project_id)
        requester_tenant_id = _tenant_id(tenant_id)
        requester_profile_id = _profile_id(profile_id)
        requested_revision = _revision(revision)
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            manifest = self._load_manifest(canonical_id)
            self._require_tenant(manifest, requester_tenant_id)
            self._require_profile(manifest, requester_profile_id)
            events = self._list_events_unlocked(manifest)
            if requested_revision > len(events):
                raise AuthoringRepositoryIntegrityError(
                    "Requested authoring snapshot revision is not committed."
                )
            event = events[requested_revision - 1]
            payload = self._load_snapshot_generation(
                project_id=canonical_id,
                tenant_id=requester_tenant_id,
                revision=requested_revision,
                snapshot_sha256=str(event.get("snapshot_sha256") or ""),
            )
            if payload.get("profile_id") != requester_profile_id:
                raise AuthoringRepositoryIntegrityError(
                    "Authoring snapshot profile does not match its manifest."
                )
            return AuthoringProject.model_validate(payload)

    def list_projects(
        self,
        *,
        tenant_id: str,
        profile_id: str | None = None,
    ) -> list[AuthoringProject]:
        requester_tenant_id = _tenant_id(tenant_id)
        requester_profile_id = _profile_id(profile_id) if profile_id is not None else None
        projects: list[AuthoringProject] = []
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            for path in sorted(self.projects_root.glob("*.json")):
                if path.is_symlink():
                    raise AuthoringRepositoryIntegrityError(
                        "Authoring manifest must not be a symbolic link."
                    )
                try:
                    project_id = _canonical_project_id(path.stem)
                    manifest = self._load_manifest(project_id)
                except AuthoringProjectNotFoundError:
                    continue
                if manifest.get("tenant_id") != requester_tenant_id:
                    continue
                if (
                    requester_profile_id is not None
                    and manifest.get("profile_id") != requester_profile_id
                ):
                    continue
                projects.append(
                    AuthoringProject.model_validate(
                        self._load_current_snapshot(manifest)
                    )
                )
        return sorted(
            projects,
            key=lambda project: (
                str(getattr(project, "updated_at", "")),
                project.project_id,
            ),
            reverse=True,
        )

    def list_events(
        self,
        project_id: str,
        *,
        tenant_id: str,
    ) -> list[dict[str, object]]:
        canonical_id = _canonical_project_id(project_id)
        requester_tenant_id = _tenant_id(tenant_id)
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            manifest = self._load_manifest(canonical_id)
            self._require_tenant(manifest, requester_tenant_id)
            return self._list_events_unlocked(manifest)

    def verify_frozen_artifact_manifest(
        self,
        project_id: str,
        *,
        tenant_id: str,
        frozen_revision: int,
        content_hash: str,
    ) -> bool:
        """Verify that a frozen artifact names a committed immutable snapshot."""

        canonical_id = _canonical_project_id(project_id)
        requester_tenant_id = _tenant_id(tenant_id)
        revision = _revision(frozen_revision)
        expected_content_hash = _content_hash(content_hash)
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            manifest = self._load_manifest(canonical_id)
            self._require_tenant(manifest, requester_tenant_id)
            if manifest.get("boundary_notice") != OFFICIAL_BOUNDARY_NOTICE:
                raise AuthoringRepositoryIntegrityError(
                    "Frozen authoring manifest has an invalid boundary notice."
                )
            if revision > _revision(manifest.get("revision")):
                return False
            events = self._list_events_unlocked(manifest)
            event = events[revision - 1]
            if event.get("content_hash") != expected_content_hash:
                return False
            snapshot = self._load_snapshot_generation(
                project_id=canonical_id,
                tenant_id=requester_tenant_id,
                revision=revision,
                snapshot_sha256=str(event.get("snapshot_sha256") or ""),
            )
            return bool(
                snapshot.get("frozen_revision") == revision
                and snapshot.get("frozen_content_hash") == expected_content_hash
                and snapshot.get("semantic_content_hash") == expected_content_hash
                and snapshot.get("boundary_notice") == OFFICIAL_BOUNDARY_NOTICE
            )

    def profile_project_count(self, profile_id: str, *, tenant_id: str) -> int:
        """Count active and partially purged projects for one tenant/profile."""

        profile = _profile_id(profile_id)
        tenant = _canonical_tenant_id(tenant_id)
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            return len(self._profile_project_ids_unlocked(profile, tenant))

    def profile_ids_with_projects(self, *, tenant_id: str) -> set[str]:
        """Return profiles owning active or retryable-purge authoring data."""

        tenant = _canonical_tenant_id(tenant_id)
        profile_ids: set[str] = set()
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            for manifest in self._iter_manifests_unlocked():
                if manifest.get("tenant_id") != tenant:
                    continue
                profile_ids.add(_profile_id(manifest.get("profile_id")))
            for tombstone in self._iter_purge_tombstones_unlocked():
                if tombstone.get("tenant_id") == tenant:
                    profile_ids.add(_profile_id(tombstone.get("profile_id")))
            for intent in self._iter_staging_intents_unlocked():
                if intent.get("tenant_id") == tenant:
                    profile_ids.add(_profile_id(intent.get("profile_id")))
        return profile_ids

    def purge_profile_projects(
        self,
        profile_id: str,
        *,
        tenant_id: str,
    ) -> AuthoringProfilePurgeResult:
        """Delete only authoring data owned by ``tenant_id`` and ``profile_id``.

        The active manifest is replaced by a content-free purge tombstone
        before immutable snapshots, events, and exports are removed. If any
        filesystem operation fails, the tombstone remains visible to
        ``profile_project_count`` so the operator can safely retry instead of
        mistaking an orphaned draft for a completed institution deletion.
        """

        profile = _profile_id(profile_id)
        tenant = _canonical_tenant_id(tenant_id)
        result = AuthoringProfilePurgeResult(profile_id=profile, tenant_id=tenant)
        with _AUTHORING_THREAD_LOCK, self._write_lock():
            project_ids = self._profile_project_ids_unlocked(profile, tenant)
            result.requested_project_count = len(project_ids)
            for project_id in project_ids:
                try:
                    self._purge_project_unlocked(
                        project_id,
                        profile_id=profile,
                        tenant_id=tenant,
                    )
                except (OSError, ValueError, AuthoringRepositoryError) as exc:
                    result.failures.append(
                        f"{project_id}: authoring project cleanup failed "
                        f"({type(exc).__name__})"
                    )
                    continue
                result.deleted_project_count += 1
        return result

    def _profile_project_ids_unlocked(
        self,
        profile_id: str,
        tenant_id: str,
    ) -> tuple[str, ...]:
        project_ids: set[str] = set()
        for manifest in self._iter_manifests_unlocked():
            if manifest.get("tenant_id") != tenant_id:
                continue
            if _profile_id(manifest.get("profile_id")) == profile_id:
                project_ids.add(_canonical_project_id(manifest.get("project_id")))
        for tombstone in self._iter_purge_tombstones_unlocked():
            if (
                tombstone.get("tenant_id") == tenant_id
                and _profile_id(tombstone.get("profile_id")) == profile_id
            ):
                project_ids.add(_canonical_project_id(tombstone.get("project_id")))
        for intent in self._iter_staging_intents_unlocked():
            if (
                intent.get("tenant_id") == tenant_id
                and _profile_id(intent.get("profile_id")) == profile_id
            ):
                project_ids.add(_canonical_project_id(intent.get("project_id")))
        return tuple(sorted(project_ids))

    def _iter_manifests_unlocked(self) -> Iterator[dict[str, object]]:
        for path in sorted(self.projects_root.glob("*.json")):
            if path.is_symlink():
                raise AuthoringRepositoryIntegrityError(
                    "Authoring manifest must not be a symbolic link."
                )
            project_id = _canonical_project_id(path.stem)
            yield self._load_manifest(project_id)

    def _iter_purge_tombstones_unlocked(self) -> Iterator[dict[str, object]]:
        for path in sorted(self.purges_root.glob("*.json")):
            yield self._load_purge_tombstone(path)

    def _iter_staging_intents_unlocked(self) -> Iterator[dict[str, object]]:
        for path in sorted(self.staging_root.glob("*.json")):
            yield self._load_staging_intent(path)

    def _load_purge_tombstone(self, path: Path) -> dict[str, object]:
        if Path(path).is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring purge tombstone must not be a symbolic link."
            )
        path = self._confined(path)
        payload = self._read_json(path, kind="authoring purge tombstone")
        if payload.get("purge_schema_version") != 1:
            raise AuthoringRepositoryIntegrityError(
                "Unsupported authoring purge tombstone schema version."
            )
        project_id = _canonical_project_id(payload.get("project_id"))
        if path.stem != project_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring purge tombstone project ID does not match its path."
            )
        _canonical_tenant_id(payload.get("tenant_id"))
        _profile_id(payload.get("profile_id"))
        return payload

    def _load_staging_intent(self, path: Path) -> dict[str, object]:
        if Path(path).is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring staging intent must not be a symbolic link."
            )
        path = self._confined(path)
        payload = self._read_json(path, kind="authoring staging intent")
        if payload.get("staging_schema_version") != 1:
            raise AuthoringRepositoryIntegrityError(
                "Unsupported authoring staging intent schema version."
            )
        project_id = _canonical_project_id(payload.get("project_id"))
        if path.stem != project_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring staging intent project ID does not match its path."
            )
        tenant_id = _canonical_tenant_id(payload.get("tenant_id"))
        profile_id = _profile_id(payload.get("profile_id"))
        if payload.get("tenant_id") != tenant_id or payload.get("profile_id") != profile_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring staging intent ownership is not canonical."
            )
        created_at = payload.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise AuthoringRepositoryIntegrityError(
                "Authoring staging intent is missing its creation time."
            )
        return payload

    def _purge_project_unlocked(
        self,
        project_id: str,
        *,
        profile_id: str,
        tenant_id: str,
    ) -> None:
        project_id = _canonical_project_id(project_id)
        tombstone_path = self.purges_root / f"{project_id}.json"
        if tombstone_path.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring purge tombstone must not be a symbolic link."
            )
        tombstone_path = self._confined(tombstone_path)
        manifest_path = self._manifest_path(project_id)
        intent_path = self._staging_intent_path(project_id)
        tombstone: dict[str, object] | None = None
        intent: dict[str, object] | None = None
        if intent_path.exists():
            intent = self._load_staging_intent(intent_path)
            if (
                intent.get("tenant_id") != tenant_id
                or _profile_id(intent.get("profile_id")) != profile_id
            ):
                raise AuthoringRepositoryIntegrityError(
                    "Authoring staging intent ownership does not match the request."
                )
        if tombstone_path.exists():
            tombstone = self._load_purge_tombstone(tombstone_path)
            if (
                tombstone.get("tenant_id") != tenant_id
                or _profile_id(tombstone.get("profile_id")) != profile_id
            ):
                raise AuthoringRepositoryIntegrityError(
                    "Authoring purge tombstone ownership does not match the request."
                )

        if manifest_path.exists():
            manifest = self._load_manifest(project_id)
            if manifest.get("tenant_id") != tenant_id:
                raise AuthoringProjectNotFoundError("Authoring project not found.")
            payload = self._load_current_snapshot(manifest)
            if _profile_id(payload.get("profile_id")) != profile_id:
                raise AuthoringProjectNotFoundError("Authoring project not found.")
            if tombstone is None:
                tombstone = {
                    "purge_schema_version": 1,
                    "project_id": project_id,
                    "tenant_id": tenant_id,
                    "profile_id": profile_id,
                    "requested_at": _utc_now().isoformat(),
                }
                self._atomic_write_json(tombstone_path, tombstone)
            # Removing the manifest first makes the draft unavailable while a
            # content-free tombstone preserves ownership and retry discovery.
            manifest_path.unlink()
        elif tombstone is None and intent is not None:
            tombstone = {
                "purge_schema_version": 1,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "requested_at": _utc_now().isoformat(),
            }
            self._atomic_write_json(tombstone_path, tombstone)
        elif tombstone is None:
            raise AuthoringProjectNotFoundError("Authoring project not found.")

        for base in (self.snapshots_root, self.events_root, self.exports_root):
            if base.is_symlink() or not base.is_dir():
                raise AuthoringRepositoryIntegrityError(
                    "Authoring storage directory must be a real directory."
                )
            self._remove_project_directory(base / project_id)
        intent_path.unlink(missing_ok=True)
        tombstone_path.unlink()

    def _recover_uncommitted_storage_unlocked(self) -> None:
        """Remove crash leftovers that have no committed manifest."""

        for intent_path in sorted(self.staging_root.glob("*.json")):
            self._recover_staging_intent_unlocked(intent_path)

        # Older builds could stage a generation without a durable intent. Once
        # the shared repository lock is held, any UUID directory without a
        # manifest or purge tombstone is unreachable and safe to remove.
        for base in (self.snapshots_root, self.events_root, self.exports_root):
            for directory in sorted(base.iterdir()):
                if directory.is_symlink():
                    raise AuthoringRepositoryIntegrityError(
                        "Authoring generation directory must not be a symbolic link."
                    )
                if not directory.is_dir():
                    continue
                project_id = _canonical_project_id(directory.name)
                if self._manifest_path(project_id).exists():
                    continue
                tombstone_path = self._confined(
                    self.purges_root / f"{project_id}.json"
                )
                if tombstone_path.exists():
                    self._load_purge_tombstone(tombstone_path)
                    continue
                self._remove_project_directory(directory)

    def _recover_staging_intent_unlocked(self, intent_path: Path) -> None:
        intent = self._load_staging_intent(intent_path)
        project_id = _canonical_project_id(intent.get("project_id"))
        manifest_path = self._manifest_path(project_id)
        if manifest_path.exists():
            manifest = self._load_manifest(project_id)
            if (
                manifest.get("tenant_id") != intent.get("tenant_id")
                or manifest.get("profile_id") != intent.get("profile_id")
            ):
                raise AuthoringRepositoryIntegrityError(
                    "Authoring staging intent ownership does not match its manifest."
                )
            intent_path.unlink(missing_ok=True)
            return
        for base in (self.snapshots_root, self.events_root, self.exports_root):
            self._remove_project_directory(base / project_id)
        intent_path.unlink(missing_ok=True)

    def _remove_project_directory(self, directory: Path) -> None:
        if Path(directory).is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring project storage must be a real directory."
            )
        directory = self._confined(directory)
        if not directory.exists() and not directory.is_symlink():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise AuthoringRepositoryIntegrityError(
                "Authoring project storage must be a real directory."
            )
        for child in directory.rglob("*"):
            if child.is_symlink():
                raise AuthoringRepositoryIntegrityError(
                    "Authoring project storage must not contain symbolic links."
                )
            self._confined(child)
        shutil.rmtree(directory)

    def _commit_generation(
        self,
        payload: dict[str, object],
        *,
        previous_manifest: dict[str, object] | None,
        actor: str,
        event_type: str,
        reason: str | None,
        event_metadata: dict[str, object],
    ) -> AuthoringProject:
        project = AuthoringProject.model_validate(payload)
        computed_content_hash = semantic_content_hash(project)
        if (
            project.semantic_content_hash is not None
            and project.semantic_content_hash != computed_content_hash
        ):
            raise AuthoringRepositoryIntegrityError(
                "Authoring semantic content hash does not match the project payload."
            )
        project = project.model_copy(
            update={"semantic_content_hash": computed_content_hash}
        )
        snapshot_payload = project.model_dump(mode="json")
        project_id = _canonical_project_id(snapshot_payload.get("project_id"))
        tenant_id = _tenant_id(snapshot_payload.get("tenant_id"))
        profile_id = _profile_id(snapshot_payload.get("profile_id"))
        revision = _revision(snapshot_payload.get("revision"))
        previous_revision = 0
        previous_event_sha256 = ""
        if previous_manifest is not None:
            previous_revision = _revision(previous_manifest.get("revision"))
            previous_event_sha256 = str(
                previous_manifest.get("event_sha256") or ""
            )
            if revision != previous_revision + 1:
                raise AuthoringRepositoryIntegrityError(
                    "Authoring revision must increase by exactly one."
                )
            if previous_manifest.get("profile_id") != profile_id:
                raise AuthoringRepositoryIntegrityError(
                    "Authoring project profile scope is immutable."
                )
        elif revision != 1:
            raise AuthoringRepositoryIntegrityError(
                "A new authoring project must start at revision 1."
            )

        snapshot_bytes = _json_bytes(snapshot_payload)
        snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        content_hash = computed_content_hash
        event_status = str(snapshot_payload.get("status") or "")
        _validate_event_metadata_context(event_metadata, status=event_status)
        now = _utc_now().isoformat()
        event: dict[str, object] = {
            "event_schema_version": 2,
            "event_id": str(uuid4()),
            "event_type": event_type,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "revision": revision,
            "previous_revision": previous_revision,
            "previous_event_sha256": previous_event_sha256,
            "snapshot_sha256": snapshot_sha256,
            "content_hash": content_hash,
            "status": event_status,
            "actor": actor,
            "reason": reason,
            "occurred_at": now,
            "metadata": event_metadata,
        }
        event["event_sha256"] = _record_sha256(event)
        event_bytes = _json_bytes(event)
        event_sha256 = str(event["event_sha256"])

        snapshot_path = self._snapshot_path(project_id, revision)
        event_path = self._event_path(project_id, revision)
        self._discard_uncommitted_generation(
            project_id,
            revision,
            previous_manifest=previous_manifest,
        )
        self._write_immutable(snapshot_path, snapshot_bytes)
        try:
            self._write_immutable(event_path, event_bytes)
            self._validate_staged_generation(
                snapshot_path=snapshot_path,
                event_path=event_path,
                snapshot_bytes=snapshot_bytes,
                event_bytes=event_bytes,
                project_id=project_id,
                tenant_id=tenant_id,
                revision=revision,
                previous_event_sha256=previous_event_sha256,
            )
        except Exception:
            event_path.unlink(missing_ok=True)
            snapshot_path.unlink(missing_ok=True)
            raise

        manifest = {
            "manifest_schema_version": 2,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "profile_id": profile_id,
            "revision": revision,
            "snapshot_sha256": snapshot_sha256,
            "event_sha256": event_sha256,
            "content_hash": content_hash,
            "boundary_notice": OFFICIAL_BOUNDARY_NOTICE,
            "frozen_revision": snapshot_payload.get("frozen_revision"),
            "frozen_content_hash": snapshot_payload.get("frozen_content_hash"),
            "committed_at": now,
        }
        try:
            self._atomic_write_json(self._manifest_path(project_id), manifest)
        except Exception:
            # These files were never made visible because the manifest replace
            # is the commit boundary. Best-effort cleanup keeps retries tidy.
            event_path.unlink(missing_ok=True)
            snapshot_path.unlink(missing_ok=True)
            raise
        return project

    def _validate_staged_generation(
        self,
        *,
        snapshot_path: Path,
        event_path: Path,
        snapshot_bytes: bytes,
        event_bytes: bytes,
        project_id: str,
        tenant_id: str,
        revision: int,
        previous_event_sha256: str,
    ) -> None:
        """Validate immutable bytes before the manifest makes them visible."""

        try:
            stored_snapshot = snapshot_path.read_bytes()
            stored_event = event_path.read_bytes()
        except OSError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Unable to re-read staged authoring generation."
            ) from exc
        if stored_snapshot != snapshot_bytes or stored_event != event_bytes:
            raise AuthoringRepositoryIntegrityError(
                "Staged authoring generation failed byte-for-byte validation."
            )
        try:
            event = _decode_json_object(stored_event)
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthoringRepositoryIntegrityError(
                "Staged authoring event is not valid JSON."
            ) from exc
        self._validate_event(
            event,
            project_id=project_id,
            tenant_id=tenant_id,
            revision=revision,
            previous_event_sha256=previous_event_sha256,
        )

    def _load_manifest(self, project_id: str) -> dict[str, object]:
        path = self._manifest_path(project_id)
        if not path.is_file():
            raise AuthoringProjectNotFoundError("Authoring project not found.")
        manifest = self._read_json(path, kind="authoring manifest")
        manifest_schema_version = manifest.get("manifest_schema_version")
        if manifest_schema_version not in {1, 2}:
            raise AuthoringRepositoryIntegrityError(
                "Unsupported authoring manifest schema version."
            )
        if manifest.get("project_id") != project_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring manifest project ID does not match its path."
            )
        tenant_id = _tenant_id(manifest.get("tenant_id"))
        revision = _revision(manifest.get("revision"))
        if manifest.get("boundary_notice") != OFFICIAL_BOUNDARY_NOTICE:
            raise AuthoringRepositoryIntegrityError(
                "Authoring manifest has an invalid boundary notice."
            )
        for manifest_field in ("snapshot_sha256", "event_sha256", "content_hash"):
            value = str(manifest.get(manifest_field) or "")
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise AuthoringRepositoryIntegrityError(
                    f"Authoring manifest has an invalid {manifest_field}."
                )
        frozen_revision = manifest.get("frozen_revision")
        frozen_content_hash = manifest.get("frozen_content_hash")
        if frozen_revision is not None:
            _revision(frozen_revision)
            _content_hash(frozen_content_hash)
        elif frozen_content_hash is not None:
            raise AuthoringRepositoryIntegrityError(
                "Authoring manifest has a frozen hash without a revision."
            )
        if manifest_schema_version == 1:
            # P0 previews created schema-v1 manifests before profile scope was
            # promoted into the content-free lookup boundary. Verify their
            # committed snapshot once, then atomically upgrade the manifest so
            # future profile filtering never needs to open unrelated drafts.
            legacy_profile_id = manifest.get("profile_id")
            if legacy_profile_id is None:
                legacy_snapshot = self._load_snapshot_generation(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    revision=revision,
                    snapshot_sha256=str(manifest.get("snapshot_sha256") or ""),
                )
                legacy_profile_id = legacy_snapshot.get("profile_id")
            canonical_profile_id = _profile_id(legacy_profile_id)
            manifest = dict(
                manifest,
                manifest_schema_version=2,
                profile_id=canonical_profile_id,
            )
            self._atomic_write_json(path, manifest)
        profile_id = _profile_id(manifest.get("profile_id"))
        if manifest.get("profile_id") != profile_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring manifest has a noncanonical profile ID."
            )
        return manifest

    def _load_current_snapshot(
        self,
        manifest: Mapping[str, object],
    ) -> dict[str, object]:
        project_id = _canonical_project_id(manifest.get("project_id"))
        tenant_id = _tenant_id(manifest.get("tenant_id"))
        profile_id = _profile_id(manifest.get("profile_id"))
        revision = _revision(manifest.get("revision"))
        payload = self._load_snapshot_generation(
            project_id=project_id,
            tenant_id=tenant_id,
            revision=revision,
            snapshot_sha256=str(manifest.get("snapshot_sha256") or ""),
        )
        expected_content_hash = str(
            payload.get("semantic_content_hash") or manifest.get("snapshot_sha256")
        )
        if manifest.get("content_hash") != expected_content_hash:
            raise AuthoringRepositoryIntegrityError(
                "Authoring manifest content hash does not match its snapshot."
            )
        if payload.get("profile_id") != profile_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring snapshot profile does not match its manifest."
            )
        if manifest.get("frozen_revision") != payload.get("frozen_revision"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring frozen revision does not match its manifest."
            )
        if manifest.get("frozen_content_hash") != payload.get("frozen_content_hash"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring frozen content hash does not match its manifest."
            )
        return payload

    def _load_snapshot_generation(
        self,
        *,
        project_id: str,
        tenant_id: str,
        revision: int,
        snapshot_sha256: str,
    ) -> dict[str, object]:
        path = self._snapshot_path(project_id, revision)
        try:
            raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        except OSError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Unable to read committed authoring snapshot."
            ) from exc
        if not raw:
            raise AuthoringRepositoryIntegrityError(
                "Committed authoring snapshot is missing."
            )
        if hashlib.sha256(raw).hexdigest() != snapshot_sha256:
            raise AuthoringRepositoryIntegrityError(
                "Committed authoring snapshot failed its integrity check."
            )
        try:
            payload = _decode_json_object(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthoringRepositoryIntegrityError(
                "Committed authoring snapshot is not valid JSON."
            ) from exc
        if payload.get("project_id") != project_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring snapshot project ID does not match its manifest."
            )
        if payload.get("tenant_id") != tenant_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring snapshot tenant does not match its manifest."
            )
        if payload.get("revision") != revision:
            raise AuthoringRepositoryIntegrityError(
                "Authoring snapshot revision does not match its manifest."
            )
        project = AuthoringProject.model_validate(payload)
        declared_content_hash = project.semantic_content_hash
        if (
            declared_content_hash is not None
            and declared_content_hash != semantic_content_hash(project)
        ):
            raise AuthoringRepositoryIntegrityError(
                "Authoring snapshot semantic content hash is invalid."
            )
        return payload

    def _list_events_unlocked(
        self,
        manifest: Mapping[str, object],
    ) -> list[dict[str, object]]:
        project_id = _canonical_project_id(manifest.get("project_id"))
        tenant_id = _tenant_id(manifest.get("tenant_id"))
        current_revision = _revision(manifest.get("revision"))
        previous_hash = ""
        events: list[dict[str, object]] = []
        for revision in range(1, current_revision + 1):
            event_path = self._event_path(project_id, revision)
            event = self._read_json(event_path, kind="authoring event")
            self._validate_event(
                event,
                project_id=project_id,
                tenant_id=tenant_id,
                revision=revision,
                previous_event_sha256=previous_hash,
            )
            previous_hash = str(event["event_sha256"])
            events.append(_content_free_event_view(event))
        if previous_hash != manifest.get("event_sha256"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring event chain does not match the committed manifest."
            )
        return events

    def _validate_event(
        self,
        event: Mapping[str, object],
        *,
        project_id: str,
        tenant_id: str,
        revision: int,
        previous_event_sha256: str,
    ) -> None:
        event_schema_version = event.get("event_schema_version")
        if type(event_schema_version) is not int or event_schema_version not in {1, 2}:
            raise AuthoringRepositoryIntegrityError(
                "Unsupported authoring event schema version."
            )
        schema_version = event_schema_version
        try:
            UUID(str(event.get("event_id") or ""))
        except (TypeError, ValueError) as exc:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event has an invalid event ID."
            ) from exc
        if event.get("project_id") != project_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event project ID does not match its path."
            )
        if event.get("tenant_id") != tenant_id:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event tenant does not match its manifest."
            )
        if event.get("revision") != revision:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event revision sequence is invalid."
            )
        if event.get("previous_revision") != revision - 1:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event previous revision is invalid."
            )
        if event.get("previous_event_sha256") != previous_event_sha256:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event hash chain is invalid."
            )
        expected_hash = _record_sha256(event)
        if event.get("event_sha256") != expected_hash:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event failed its integrity check."
            )
        try:
            event_type = _event_type(event.get("event_type"))
            actor = _actor_id(event.get("actor"))
            reason = (
                _legacy_event_reason(event.get("reason"))
                if schema_version == 1
                else _event_reason(event.get("reason"))
            )
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                raise AuthoringRepositoryIntegrityError(
                    "Authoring event metadata must be a JSON object."
                )
        except ValueError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event contains invalid canonical fields."
            ) from exc
        try:
            safe_metadata = _safe_event_metadata(
                metadata,
                schema_version=schema_version,
            )
        except ValueError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event metadata contains invalid canonical fields."
            ) from exc
        if event_type != event.get("event_type"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring event type is not canonical."
            )
        if actor != event.get("actor"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring event actor is not canonical."
            )
        if reason != event.get("reason"):
            raise AuthoringRepositoryIntegrityError(
                "Authoring event reason is not canonical."
            )
        status = str(event.get("status") or "")
        if status not in {item.value for item in AuthoringProjectStatus}:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event status is invalid."
            )
        if schema_version == 2:
            try:
                _validate_event_metadata_context(safe_metadata, status=status)
            except ValueError as exc:
                raise AuthoringRepositoryIntegrityError(
                    "Authoring event metadata does not match its event context."
                ) from exc
        if safe_metadata != dict(metadata):
            raise AuthoringRepositoryIntegrityError(
                "Authoring event metadata is not canonical."
            )
        _content_hash(event.get("snapshot_sha256"))
        _content_hash(event.get("content_hash"))
        if "reason" not in event:
            raise AuthoringRepositoryIntegrityError(
                "Authoring event is missing its reason field."
            )

    def _require_tenant(
        self,
        manifest: Mapping[str, object],
        tenant_id: str,
    ) -> None:
        if manifest.get("tenant_id") != tenant_id:
            # Deliberately indistinguishable from a missing UUID.
            raise AuthoringProjectNotFoundError("Authoring project not found.")

    def _require_not_purging_unlocked(self, project_id: str) -> None:
        tombstone_path = self._confined(
            self.purges_root / f"{_canonical_project_id(project_id)}.json"
        )
        if not tombstone_path.exists():
            return
        self._load_purge_tombstone(tombstone_path)
        # A valid tombstone is a deletion barrier even when a failed unlink
        # temporarily left the old manifest in place.
        raise AuthoringProjectNotFoundError("Authoring project not found.")

    def _require_profile(
        self,
        manifest: Mapping[str, object],
        profile_id: str,
    ) -> None:
        if manifest.get("profile_id") != profile_id:
            # Deliberately indistinguishable from a missing UUID.
            raise AuthoringProjectNotFoundError("Authoring project not found.")

    def _discard_uncommitted_generation(
        self,
        project_id: str,
        revision: int,
        *,
        previous_manifest: Mapping[str, object] | None,
    ) -> None:
        committed_revision = (
            _revision(previous_manifest.get("revision"))
            if previous_manifest is not None
            else 0
        )
        if revision <= committed_revision:
            raise AuthoringRepositoryIntegrityError(
                "Refusing to replace a committed authoring generation."
            )
        self._snapshot_path(project_id, revision).unlink(missing_ok=True)
        self._event_path(project_id, revision).unlink(missing_ok=True)

    def _manifest_path(self, project_id: str) -> Path:
        return self._confined(self.projects_root / f"{project_id}.json")

    def _staging_intent_path(self, project_id: str) -> Path:
        return self._confined(self.staging_root / f"{project_id}.json")

    def _snapshot_path(self, project_id: str, revision: int) -> Path:
        directory = self._project_generation_dir(self.snapshots_root, project_id)
        return self._confined(directory / f"{revision:020d}.json")

    def _event_path(self, project_id: str, revision: int) -> Path:
        directory = self._project_generation_dir(self.events_root, project_id)
        return self._confined(directory / f"{revision:020d}.json")

    def _export_artifact_path_unlocked(
        self,
        project_id: str,
        *,
        frozen_revision: int,
        content_hash: str,
        suffix: str,
    ) -> Path:
        project_directory = self._project_generation_dir(
            self.exports_root,
            project_id,
        )
        revision_directory = self._confined(
            project_directory / f"{frozen_revision:020d}"
        )
        if revision_directory.exists() and revision_directory.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring export revision directory must not be a symbolic link."
            )
        revision_directory.mkdir(parents=False, exist_ok=True)
        return self._confined(revision_directory / f"{content_hash}.{suffix}")

    def _write_export_artifact_unlocked(
        self,
        path: Path,
        content: bytes,
    ) -> tuple[bool, bytes]:
        path = self._confined(path)
        if path.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring export must not be a symbolic link."
            )
        if path.exists():
            stored = path.read_bytes()
            if stored != content:
                raise AuthoringRepositoryIntegrityError(
                    "An incompatible authoring export already exists."
                )
            return False, stored
        tmp_path = self._confined(
            path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        )
        try:
            try:
                with tmp_path.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                _restrict_permissions(tmp_path)
                os.replace(tmp_path, path)
                stored = path.read_bytes()
                if stored != content:
                    raise AuthoringRepositoryIntegrityError(
                        "Authoring export did not survive atomic storage revalidation."
                    )
                _restrict_permissions(path)
            except BaseException:
                # The target did not exist before this request. If replace won
                # and any later validation or interruption failed, it is always
                # this request's uncommitted artifact and is safe to remove.
                path.unlink(missing_ok=True)
                raise
        finally:
            tmp_path.unlink(missing_ok=True)
        return True, stored

    def _project_generation_dir(self, base: Path, project_id: str) -> Path:
        directory = self._confined(base / project_id)
        if directory.exists() and directory.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring generation directory must not be a symbolic link."
            )
        directory.mkdir(parents=True, exist_ok=True)
        return self._confined(directory)

    def _confined(self, path: Path) -> Path:
        if self.root.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring storage root must not be a symbolic link."
            )
        resolved_root = self.root.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Authoring storage path escaped its isolated root."
            ) from exc
        return resolved

    def _initialize_storage(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring storage root must not be a symbolic link."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.projects_root,
            self.snapshots_root,
            self.events_root,
            self.exports_root,
            self.staging_root,
            self.purges_root,
        ):
            if directory.exists() and directory.is_symlink():
                raise AuthoringRepositoryIntegrityError(
                    "Authoring storage directory must not be a symbolic link."
                )
            directory.mkdir(parents=True, exist_ok=True)
            self._confined(directory)

    def _read_json(self, path: Path, *, kind: str) -> dict[str, object]:
        path = self._confined(path)
        if path.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                f"{kind.capitalize()} must not be a symbolic link."
            )
        try:
            return _decode_json_object(path.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AuthoringRepositoryIntegrityError(
                f"Unable to read committed {kind}."
            ) from exc

    def _write_immutable(self, path: Path, payload: bytes) -> None:
        path = self._confined(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Refusing to replace an immutable authoring record."
            ) from exc
        _restrict_permissions(path)

    def _atomic_write_json(
        self,
        path: Path,
        payload: Mapping[str, object],
    ) -> None:
        path = self._confined(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._confined(
            path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        )
        try:
            with tmp_path.open("xb") as handle:
                handle.write(_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_permissions(tmp_path)
            _replace_with_retry(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        lock_path = self._confined(self.root / ".write.lock")
        if lock_path.exists() and lock_path.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "Authoring repository lock must not be a symbolic link."
            )
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)


def _project_payload(project: AuthoringProject) -> dict[str, object]:
    if not isinstance(project, AuthoringProject):
        project = AuthoringProject.model_validate(project)
    return dict(project.model_dump(mode="json"))


def _canonical_project_id(value: object) -> str:
    candidate = str(value or "")
    if candidate != candidate.strip() or not candidate:
        raise ValueError("project_id must be a canonical UUID.")
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise ValueError("project_id must be a canonical UUID.") from exc
    canonical = str(parsed)
    if candidate != canonical:
        raise ValueError("project_id must be a canonical lowercase UUID.")
    return canonical


def _tenant_id(value: object) -> str:
    return _canonical_tenant_id(value)


def _canonical_tenant_id(value: object) -> str:
    try:
        return tenant_directory_key(str(value or ""))
    except ValueError as exc:
        raise ValueError("tenant_id must be a canonical lowercase tenant ID.") from exc


def _profile_id(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if (
        not candidate
        or len(candidate) > INSTITUTION_PROFILE_ID_MAX_LENGTH
        or re.fullmatch(INSTITUTION_PROFILE_ID_PATTERN, candidate) is None
    ):
        raise ValueError("profile_id must be a non-empty canonical identifier.")
    return candidate


def _actor_id(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 120 or any(char in candidate for char in "\r\n"):
        raise ValueError("actor must be a non-empty single-line identifier.")
    return candidate


def _event_type(value: object) -> str:
    candidate = str(value or "").strip().lower()
    allowed = {event_type.value for event_type in AuthoringEventType}
    if candidate not in allowed:
        raise ValueError("event_type must be a supported authoring event name.")
    return candidate


def _event_reason(value: object) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if len(candidate) > 2000 or any(char in candidate for char in "\r\n"):
        raise ValueError("reason must be a short, single-line value.")
    # Immutable events deliberately record only whether a reason was supplied.
    # Free text belongs in the tenant-scoped project snapshot, never the
    # content-free event chain.
    return "provided"


def _legacy_event_reason(value: object) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if len(candidate) > 2000 or any(char in candidate for char in "\r\n"):
        raise ValueError("reason must be a short, single-line value.")
    return candidate


def _content_free_event_view(event: dict[str, object]) -> dict[str, object]:
    """Project legacy free text and unknown metadata out of the read model."""

    if event.get("event_schema_version") != 1:
        return event
    raw_reason = _legacy_event_reason(event.get("reason"))
    raw_metadata = event.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    visible_metadata: dict[str, object] = {}
    metadata_redacted = False
    for key, value in metadata.items():
        try:
            visible_metadata.update(
                _safe_event_metadata(
                    {key: value},
                    schema_version=2,
                    enforce_relationships=False,
                )
            )
        except ValueError:
            metadata_redacted = True

    reason_redacted = raw_reason is not None and raw_reason != "provided"
    if raw_reason is not None and raw_reason != "provided":
        visible_metadata["reason_supplied"] = True
        visible_metadata["reason_sha256"] = hashlib.sha256(
            raw_reason.encode("utf-8")
        ).hexdigest()
    if not reason_redacted and not metadata_redacted:
        return event

    projected = dict(
        event,
        reason="provided" if reason_redacted else raw_reason,
        metadata=visible_metadata,
        read_model_redacted=True,
    )
    if metadata_redacted:
        projected["legacy_metadata_redacted"] = True
        projected["legacy_metadata_sha256"] = hashlib.sha256(
            _json_bytes(metadata)
        ).hexdigest()
    return projected


def _content_hash(value: object) -> str:
    candidate = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", candidate):
        raise AuthoringRepositoryIntegrityError(
            "Authoring content hash must be a lowercase SHA-256 value."
        )
    return candidate


def _revision(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("revision must be an integer.")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"revision must be at least {minimum}.")
    return value


def _safe_event_metadata(
    metadata: Mapping[str, object] | None,
    *,
    schema_version: int = 2,
    enforce_relationships: bool = True,
) -> dict[str, object]:
    if schema_version == 1:
        return _legacy_event_metadata(metadata)
    if schema_version != 2:
        raise ValueError("Unsupported authoring event metadata schema version.")

    safe: dict[str, object] = {}
    for raw_key, raw_value in dict(metadata or {}).items():
        key = str(raw_key or "").strip().lower()
        if not _EVENT_METADATA_KEY_RE.fullmatch(key):
            raise ValueError("Event metadata keys must be canonical identifiers.")
        key_parts = set(filter(None, re.split(r"[_.-]+", key)))
        if key_parts & _SENSITIVE_EVENT_KEY_PARTS:
            raise ValueError("Draft content is not allowed in authoring events.")
        if key not in _ALLOWED_EVENT_METADATA_KEYS:
            raise ValueError("Unsupported authoring event metadata key.")
        value = _validated_event_metadata_value(key, raw_value)
        safe[key] = value
    if enforce_relationships:
        _validate_event_metadata_relationships(safe)
    return safe


def _legacy_event_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    """Validate the historical v1 shape without applying v2's new allowlist."""

    safe: dict[str, object] = {}
    for raw_key, raw_value in dict(metadata or {}).items():
        key = str(raw_key or "").strip().lower()
        if not _EVENT_METADATA_KEY_RE.fullmatch(key):
            raise ValueError("Event metadata keys must be canonical identifiers.")
        key_parts = set(filter(None, re.split(r"[_.-]+", key)))
        if key_parts & _LEGACY_SENSITIVE_EVENT_KEY_PARTS:
            raise ValueError("Draft content is not allowed in authoring events.")
        if raw_value is None or type(raw_value) in {bool, int, float}:
            value: object = raw_value
        elif isinstance(raw_value, str):
            value = raw_value.strip()
            if len(value) > 200 or any(char in value for char in "\r\n"):
                raise ValueError(
                    "Event metadata strings must be short, single-line values."
                )
        else:
            raise ValueError("Event metadata values must be scalar JSON values.")
        safe[key] = value
    return safe


def _validated_event_metadata_value(key: str, raw_value: object) -> object:
    if key in {"from_status", "to_status"}:
        value = _event_metadata_string(raw_value)
        if value not in {status.value for status in AuthoringProjectStatus}:
            raise ValueError("Event status metadata must use a supported status.")
        return value
    if key in {"file_sha256", "reason_sha256"}:
        value = _event_metadata_string(raw_value)
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Event hash metadata must be lowercase SHA-256.")
        return value
    if key in {"migration", "reason_supplied", "self_freeze", "training_only"}:
        if type(raw_value) is not bool:
            raise ValueError("Event boolean metadata must use a JSON boolean.")
        return raw_value
    if key in {"lint_error_count", "lint_warning_count"}:
        if type(raw_value) is not int or raw_value < 0:
            raise ValueError("Event count metadata must be a non-negative integer.")
        return raw_value
    if key == "export_format":
        value = _event_metadata_string(raw_value)
        if value not in {"json", "markdown"}:
            raise ValueError("Event export format metadata is invalid.")
        return value
    if key == "template_id_value":
        value = _event_metadata_string(raw_value)
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value) is None:
            raise ValueError("Event template ID metadata is invalid.")
        return value
    raise ValueError("Unsupported authoring event metadata key.")


def _event_metadata_string(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        raise ValueError("Event metadata value must be a string.")
    value = raw_value.strip()
    if not value or len(value) > 200 or any(char in value for char in "\r\n"):
        raise ValueError("Event metadata strings must be short, single-line values.")
    return value


def _validate_event_metadata_relationships(metadata: Mapping[str, object]) -> None:
    for left, right in (
        ("from_status", "to_status"),
        ("reason_supplied", "reason_sha256"),
        ("export_format", "file_sha256"),
        ("self_freeze", "training_only"),
        ("lint_error_count", "lint_warning_count"),
    ):
        if (left in metadata) != (right in metadata):
            raise ValueError(
                f"Event metadata fields {left} and {right} must be supplied together."
            )


def _validate_event_metadata_context(
    metadata: Mapping[str, object],
    *,
    status: str,
) -> None:
    _validate_event_metadata_relationships(metadata)
    to_status = metadata.get("to_status")
    if to_status is not None and to_status != status:
        raise ValueError("Event to_status metadata must match the snapshot status.")


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _decode_json_object(raw: bytes) -> dict[str, object]:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _record_sha256(record: Mapping[str, object]) -> str:
    unsigned = dict(record)
    unsigned.pop("event_sha256", None)
    return hashlib.sha256(_json_bytes(unsigned)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for the authoring repository lock."
                    ) from exc
                time.sleep(_LOCK_POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _replace_with_retry(source: Path, target: Path) -> None:
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if (
                exc.errno not in {errno.EACCES, errno.EPERM}
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_REPLACE_RETRY_INTERVAL_SECONDS)
