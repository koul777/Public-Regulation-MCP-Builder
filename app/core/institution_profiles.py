from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


ALLOWED_REQUIRED_ROW_FIELDS = frozenset(
    {
        "institution_name",
        "apba_id",
        "source_system",
        "source_url",
        "source_record_id",
        "source_file_id",
        "source_disclosure_date",
        "source_posted_date",
        "profile_id",
    }
)
INSTITUTION_PROFILE_FIELDS = frozenset(
    {
        "display_name",
        "institution_name",
        "tenant_id",
        "apba_id",
        "source_system",
        "source_url",
        "required_row_fields",
        "max_upload_mb",
        "notes",
    }
)
REGISTRY_FIELDS = frozenset({"default_profile_id", "profiles"})


@dataclass(frozen=True)
class InstitutionProfile:
    profile_id: str
    display_name: str = ""
    institution_name: str | None = None
    tenant_id: str | None = None
    apba_id: str | None = None
    source_system: str | None = None
    source_url: str | None = None
    required_row_fields: tuple[str, ...] = ()
    max_upload_mb: int | None = None
    notes: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "institution_name": self.institution_name,
            "tenant_id": self.tenant_id,
            "apba_id": self.apba_id,
            "source_system": self.source_system,
            "source_url": self.source_url,
            "required_row_fields": list(self.required_row_fields),
            "max_upload_mb": self.max_upload_mb,
        }


@dataclass(frozen=True)
class InstitutionProfileRegistry:
    profiles: dict[str, InstitutionProfile]
    default_profile_id: str | None = None
    sha256: str = ""

    def resolve(self, profile_id: str | None, *, strict: bool = False) -> InstitutionProfile | None:
        normalized = normalize_profile_id(profile_id or self.default_profile_id)
        if not normalized:
            if strict:
                raise ValueError("Institution profile_id is required.")
            return None
        profile = self.profiles.get(normalized)
        if profile is None and strict:
            raise ValueError(f"Unknown institution profile_id: {profile_id}")
        return profile

    def required_row_fields_for(self, profile_id: str | None, *, strict: bool = False) -> tuple[str, ...]:
        profile = self.resolve(profile_id, strict=strict)
        return profile.required_row_fields if profile else ()

    def summary(self) -> dict[str, Any]:
        return {
            "default_profile_id": self.default_profile_id,
            "profile_count": len(self.profiles),
            "profile_ids": [profile.profile_id for profile in sorted(self.profiles.values(), key=lambda item: item.profile_id)],
            "sha256": self.sha256,
        }


def apply_institution_profile_to_metadata(
    metadata: dict[str, Any],
    registry: InstitutionProfileRegistry,
    *,
    strict: bool = False,
    enforce_required: bool = False,
) -> dict[str, Any]:
    resolved = dict(metadata)
    profile_id = resolved.get("profile_id") or registry.default_profile_id
    profile = registry.resolve(profile_id, strict=strict)
    if profile is not None:
        resolved["profile_id"] = profile.profile_id
        for field in ("institution_name", "apba_id", "source_system", "source_url"):
            if not resolved.get(field):
                resolved[field] = getattr(profile, field)
        if enforce_required:
            missing = [field for field in profile.required_row_fields if resolved.get(field) in (None, "")]
            if missing:
                raise ValueError(
                    f"Institution profile '{profile.profile_id}' requires fields: {', '.join(missing)}"
                )
    elif profile_id:
        resolved["profile_id"] = profile_id
    return resolved


def normalize_profile_id(profile_id: str | None) -> str:
    return str(profile_id or "").strip().lower()


def load_institution_profile_registry(path: str | Path | None) -> InstitutionProfileRegistry:
    if not path:
        return InstitutionProfileRegistry(profiles={})
    registry_path = Path(path).expanduser()
    if not registry_path.exists():
        raise FileNotFoundError(f"Institution profile registry not found: {registry_path}")
    content = registry_path.read_bytes()
    return load_institution_profile_registry_from_bytes(content)


def load_institution_profile_registry_from_bytes(content: bytes) -> InstitutionProfileRegistry:
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Institution profile registry must be a JSON object.")
    unknown = sorted(set(raw) - REGISTRY_FIELDS)
    if unknown:
        raise ValueError(f"Institution profile registry has unknown fields: {', '.join(unknown)}")
    profiles = _profiles_from_mapping(raw.get("profiles", {}))
    default_profile_id = _optional_string(raw.get("default_profile_id"), field="default_profile_id")
    if default_profile_id and normalize_profile_id(default_profile_id) not in profiles:
        raise ValueError(f"default_profile_id is not registered: {default_profile_id}")
    return InstitutionProfileRegistry(
        profiles=profiles,
        default_profile_id=default_profile_id,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def institution_profile_registry_to_dict(registry: InstitutionProfileRegistry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "profiles": {
            profile.profile_id: _profile_to_dict(profile)
            for profile in sorted(registry.profiles.values(), key=lambda item: item.profile_id)
        }
    }
    if registry.default_profile_id:
        payload["default_profile_id"] = registry.default_profile_id
    return payload


def institution_profile_registry_to_bytes(registry: InstitutionProfileRegistry) -> bytes:
    content = json.dumps(
        institution_profile_registry_to_dict(registry),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"{content}\n".encode("utf-8")


def save_institution_profile_registry(
    path: str | Path,
    registry: InstitutionProfileRegistry,
    *,
    backup_existing: bool = True,
) -> dict[str, Any]:
    registry_path = Path(path).expanduser()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    content = institution_profile_registry_to_bytes(registry)
    # Validate the exact bytes that will be persisted before replacing the file.
    saved_registry = load_institution_profile_registry_from_bytes(content)
    backup_path: Path | None = None
    if backup_existing and registry_path.exists():
        backup_path = _next_backup_path(registry_path)
        shutil.copy2(registry_path, backup_path)
    tmp_path = registry_path.with_name(f".{registry_path.name}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, registry_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return {
        "path": str(registry_path),
        "backup_path": str(backup_path) if backup_path else None,
        "sha256": saved_registry.sha256,
        "profile_count": len(saved_registry.profiles),
        "default_profile_id": saved_registry.default_profile_id,
    }


def upsert_institution_profile(
    registry: InstitutionProfileRegistry,
    profile_id: str,
    *,
    display_name: str | None = None,
    institution_name: str | None = None,
    tenant_id: str | None = None,
    apba_id: str | None = None,
    source_system: str | None = None,
    source_url: str | None = None,
    required_row_fields: list[str] | tuple[str, ...] | None = None,
    max_upload_mb: int | None = None,
    notes: str | None = None,
    make_default: bool = False,
) -> InstitutionProfileRegistry:
    cleaned_profile_id = str(profile_id or "").strip()
    if not cleaned_profile_id:
        raise ValueError("Institution profile id must not be empty.")
    normalized = normalize_profile_id(cleaned_profile_id)
    existing = registry.profiles.get(normalized)
    stored_profile_id = existing.profile_id if existing else cleaned_profile_id
    raw = institution_profile_registry_to_dict(registry)
    profiles = raw.setdefault("profiles", {})
    profiles[stored_profile_id] = {
        key: value
        for key, value in {
            "display_name": _clean_optional_string(display_name),
            "institution_name": _clean_optional_string(institution_name),
            "tenant_id": _clean_optional_string(tenant_id),
            "apba_id": _clean_optional_string(apba_id),
            "source_system": _clean_optional_string(source_system),
            "source_url": _clean_optional_string(source_url),
            "required_row_fields": list(required_row_fields or ()),
            "max_upload_mb": max_upload_mb,
            "notes": _clean_optional_string(notes),
        }.items()
        if value not in (None, "", [])
    }
    if make_default:
        raw["default_profile_id"] = stored_profile_id
    elif raw.get("default_profile_id") and normalize_profile_id(str(raw["default_profile_id"])) not in {
        normalize_profile_id(key) for key in profiles
    }:
        raw.pop("default_profile_id", None)
    validated = load_institution_profile_registry_from_bytes(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return load_institution_profile_registry_from_bytes(institution_profile_registry_to_bytes(validated))


def delete_institution_profile(
    registry: InstitutionProfileRegistry,
    profile_id: str,
) -> InstitutionProfileRegistry:
    """Remove one profile and select a deterministic replacement default when needed."""
    normalized = normalize_profile_id(profile_id)
    if not normalized:
        raise ValueError("Institution profile id must not be empty.")

    raw = institution_profile_registry_to_dict(registry)
    profiles = raw.get("profiles", {})
    stored_profile_id = next(
        (candidate for candidate in profiles if normalize_profile_id(candidate) == normalized),
        None,
    )
    if stored_profile_id is None:
        raise ValueError(f"Unknown institution profile_id: {profile_id}")
    profiles.pop(stored_profile_id)

    if normalize_profile_id(raw.get("default_profile_id")) == normalized:
        if profiles:
            raw["default_profile_id"] = sorted(profiles, key=normalize_profile_id)[0]
        else:
            raw.pop("default_profile_id", None)

    validated = load_institution_profile_registry_from_bytes(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return load_institution_profile_registry_from_bytes(institution_profile_registry_to_bytes(validated))


def _profiles_from_mapping(raw_profiles: Any) -> dict[str, InstitutionProfile]:
    if raw_profiles in (None, {}):
        return {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("Institution profile registry 'profiles' must be a JSON object.")
    profiles: dict[str, InstitutionProfile] = {}
    original_keys: dict[str, str] = {}
    for raw_profile_id, profile_data in raw_profiles.items():
        profile_id = str(raw_profile_id)
        normalized = normalize_profile_id(profile_id)
        if not normalized:
            raise ValueError("Institution profile id must not be empty.")
        if normalized != profile_id.lower():
            raise ValueError(f"Institution profile id must not contain leading or trailing whitespace: {profile_id!r}")
        if normalized in profiles:
            raise ValueError(
                "Institution profile ids collide after normalization: "
                f"{original_keys[normalized]!r}, {profile_id!r}"
            )
        profiles[normalized] = _profile_from_mapping(profile_id, profile_data)
        original_keys[normalized] = profile_id
    return profiles


def _profile_to_dict(profile: InstitutionProfile) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in ("display_name", "institution_name", "tenant_id", "apba_id", "source_system", "source_url", "notes"):
        value = getattr(profile, field)
        if value:
            payload[field] = value
    if profile.required_row_fields:
        payload["required_row_fields"] = list(profile.required_row_fields)
    if profile.max_upload_mb is not None:
        payload["max_upload_mb"] = profile.max_upload_mb
    return payload


def _next_backup_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _profile_from_mapping(profile_id: str, raw: Any) -> InstitutionProfile:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"Institution profile '{profile_id}' must be a JSON object.")
    unknown = sorted(set(raw) - INSTITUTION_PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Institution profile '{profile_id}' has unknown fields: {', '.join(unknown)}")
    max_upload_mb = raw.get("max_upload_mb")
    if max_upload_mb is not None:
        if isinstance(max_upload_mb, bool) or not isinstance(max_upload_mb, int) or max_upload_mb <= 0:
            raise ValueError(f"Institution profile '{profile_id}' max_upload_mb must be a positive integer.")
    return InstitutionProfile(
        profile_id=profile_id,
        display_name=_optional_string(raw.get("display_name"), field=f"{profile_id}.display_name") or "",
        institution_name=_optional_string(raw.get("institution_name"), field=f"{profile_id}.institution_name"),
        tenant_id=_optional_string(raw.get("tenant_id"), field=f"{profile_id}.tenant_id"),
        apba_id=_optional_string(raw.get("apba_id"), field=f"{profile_id}.apba_id"),
        source_system=_optional_string(raw.get("source_system"), field=f"{profile_id}.source_system"),
        source_url=_optional_string(raw.get("source_url"), field=f"{profile_id}.source_url"),
        required_row_fields=_required_row_fields(raw.get("required_row_fields"), profile_id=profile_id),
        max_upload_mb=max_upload_mb,
        notes=_optional_string(raw.get("notes"), field=f"{profile_id}.notes") or "",
    )


def _optional_string(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    return value.strip() or None


def _clean_optional_string(value: str | None) -> str | None:
    return str(value or "").strip() or None


def _required_row_fields(value: Any, *, profile_id: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ValueError(f"Institution profile '{profile_id}' required_row_fields must be a list.")
    fields: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Institution profile '{profile_id}' required_row_fields entries must be strings.")
        field = item.strip()
        if not field:
            raise ValueError(f"Institution profile '{profile_id}' required_row_fields must not contain empty entries.")
        if field not in ALLOWED_REQUIRED_ROW_FIELDS:
            allowed = ", ".join(sorted(ALLOWED_REQUIRED_ROW_FIELDS))
            raise ValueError(
                f"Institution profile '{profile_id}' has unsupported required_row_field {field!r}. "
                f"Allowed fields: {allowed}"
            )
        if field in seen:
            raise ValueError(f"Institution profile '{profile_id}' has duplicate required_row_field {field!r}.")
        seen.add(field)
        fields.append(field)
    return tuple(fields)
