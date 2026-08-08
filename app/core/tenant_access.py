from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.config import Settings


def tenant_scoped_value_matches(resource_tenant_id: str | None, requester_tenant_id: str | None) -> bool:
    if resource_tenant_id in (None, ""):
        return False
    return resource_tenant_id == requester_tenant_id


def resource_visible_to_tenant(resource: Any, requester_tenant_id: str | None) -> bool:
    return tenant_scoped_value_matches(getattr(resource, "tenant_id", None), requester_tenant_id)


def tenant_storage_key(tenant_id: str | None) -> str:
    raw = str(tenant_id or "default").strip() or "default"
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return key or "default"


def institution_storage_dirname(profile_id: str | None) -> str:
    """기관별 보조 폴더(대기 업로드, 저장한 작업) 이름.

    쓰는 쪽과 지우는 쪽이 이름을 따로 계산하면 지우는 쪽이 폴더를 찾지 못한다. 그러면
    기관을 지워도 대기 파일이 남고, 기관 ID는 기관명 해시라 같은 이름으로 다시 등록할
    때 그 파일들이 그대로 붙는다. 이름은 반드시 이 함수 하나로만 만든다.
    """
    digest = hashlib.sha256(str(profile_id or "").strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"institution-{digest}"


INSTITUTION_STORAGE_MARKER = "institution_profile.json"


def institution_storage_dir(root: Path, profile_id: str | None, *, create: bool = False) -> Path:
    """기관별 보조 폴더 경로. 만들 때 어느 기관 것인지 폴더 안에 적어 둔다.

    폴더 이름은 기관 ID의 해시라 이름만 보고는 되짚을 수 없다. 적어 두지 않으면 프로필과
    문서를 모두 지운 뒤 남은 폴더가 어느 기관 것인지 알 방법이 없어, 같은 이름으로 다시
    등록하기 전까지 화면 어디에도 드러나지 않는다.
    """
    directory = Path(root) / institution_storage_dirname(profile_id)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / INSTITUTION_STORAGE_MARKER
        if not marker.exists():
            marker.write_text(
                json.dumps(
                    {"profile_id": str(profile_id or "").strip().lower()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    return directory


def institution_profile_id_from_storage_dir(directory: Path) -> str:
    """보조 폴더에 적어 둔 기관 ID. 표식이 없으면 빈 문자열."""
    try:
        payload = json.loads(
            (Path(directory) / INSTITUTION_STORAGE_MARKER).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("profile_id") or "").strip().lower()


def settings_for_tenant(settings: Settings, tenant_id: str | None) -> Settings:
    if not settings.tenant_storage_isolation:
        return settings
    return replace(settings, data_dir=settings.data_dir / "tenants" / tenant_storage_key(tenant_id))
