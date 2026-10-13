"""Local operator Vercel workflow. No shell commands or credentials in UI state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.generate_mcp_client_config import validate_mcp_runtime_data_bundle_integrity
from scripts.prepare_vercel_mcp_deployment import prepare_vercel_mcp_deployment
from scripts.run_mcp_client_config_smoke import run_mcp_client_config_smoke

CLI_VERSION = "62.1.0"
Progress = Callable[[str], None]
_DEPLOY_LOCK = threading.Lock()


class DeploymentError(RuntimeError):
    """An actionable, credential-free operator message."""

    def __init__(self, message: str, *, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code


def _tools_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "pr-mcp-builder" / "tools" / "vercel"


def vercel_command() -> list[str]:
    """Invoke npm's JS entrypoint directly, including on Windows with spaces."""
    node = shutil.which("node")
    if not node:
        raise DeploymentError("Node.js가 필요합니다. Node.js LTS 설치 후 프로그램을 다시 여세요.", code="node_missing")
    candidates = [_tools_dir() / "node_modules" / "vercel" / "dist" / "index.js"]
    executable = shutil.which("vercel")
    if executable:
        path = Path(executable).resolve()
        if path.suffix == ".js":
            candidates.append(path)
        candidates.extend([
            path.parent / "node_modules" / "vercel" / "dist" / "index.js",
            path.parent.parent / "lib" / "node_modules" / "vercel" / "dist" / "index.js",
        ])
    for candidate in candidates:
        if candidate.is_file():
            return [node, str(candidate)]
    raise DeploymentError("Vercel 배포 도구를 설치해야 합니다. 아래 설치 버튼을 누르세요.", code="cli_missing")


def _environment(cwd: Path | None = None) -> dict[str, str]:
    # Do not inherit a project link, a CI credential or MCP secrets from the builder.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("VERCEL_", "MCP_", "GIT_"))}
    env.update({"NO_COLOR": "1", "VERCEL_TELEMETRY_DISABLED": "1"})
    if cwd is not None:
        # `vercel link --yes` can otherwise discover the builder's parent Git
        # repository and connect its origin automatically. Git ignores a ceiling
        # equal to its starting directory, so stop traversal at the parent.
        env["GIT_CEILING_DIRECTORIES"] = str(cwd.resolve().parent)
    return env


def _run(args: list[str], *, cwd: Path, timeout: int = 90) -> str:
    try:
        result = subprocess.run(
            [*vercel_command(), *args, "--no-color", "--non-interactive"],
            cwd=cwd, env=_environment(cwd), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise DeploymentError("Vercel 응답 시간이 초과되었습니다. 배포 중이었다면 Vercel 대시보드에서 결과를 먼저 확인하세요.", code="timeout") from None
    except OSError:
        raise DeploymentError("배포 도구를 실행하지 못했습니다. Node.js와 Vercel 도구 설치 상태를 확인하세요.") from None
    if result.returncode:
        detail = (result.stderr + result.stdout).lower()
        if any(term in detail for term in ("unknown option", "unknown or unexpected option",
                                            "unrecognized option", "unexpected option")):
            raise DeploymentError("Vercel 배포 도구가 이 프로그램의 연결 방식을 지원하지 않습니다. 배포 도구를 업데이트하고 다시 연결하세요.", code="cli_outdated")
        if any(term in detail for term in ("login", "log in", "invalid token", "token expired", "no credentials", "unauthorized")):
            raise DeploymentError("Vercel 로그인이 필요하거나 만료되었습니다. 계정을 다시 연결하세요.", code="login_required")
        if any(term in detail for term in ("enotfound", "econnreset", "etimedout", "network error", "fetch failed")):
            raise DeploymentError("Vercel에 접속하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.", code="network")
        if "not found" in detail or "permission" in detail or "forbidden" in detail:
            raise DeploymentError("선택한 Vercel 팀·프로젝트에 접근할 수 없습니다. 계정과 프로젝트를 다시 선택하세요.", code="project_access")
        raise DeploymentError("Vercel 작업이 실패했습니다. 배포 상태·로그에서 원인을 확인한 뒤 다시 시도하세요.", code="operation_failed")
    return result.stdout.strip()


def _json(args: list[str], *, cwd: Path) -> dict[str, Any]:
    try:
        result = json.loads(_run([*args, "--json"], cwd=cwd))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise DeploymentError("Vercel 응답을 읽지 못했습니다. 배포 도구를 설치·업데이트한 뒤 다시 시도하세요.", code="cli_outdated") from None


def install_vercel(progress: Progress) -> dict[str, Any]:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise DeploymentError("Node.js LTS를 먼저 설치하고 프로그램을 다시 여세요.")
    npm_path = Path(npm).resolve()
    candidates = [npm_path.parent / "node_modules/npm/bin/npm-cli.js", npm_path]
    entry = next((p for p in candidates if p.is_file() and p.suffix == ".js"), None)
    if not entry:
        raise DeploymentError("npm 실행 파일을 찾지 못했습니다. Node.js LTS를 다시 설치하세요.")
    progress("Vercel 배포 도구 설치 중 — 처음 한 번만 필요합니다.")
    try:
        result = subprocess.run(
            [node, str(entry), "install", "--prefix", str(_tools_dir()),
             f"vercel@{CLI_VERSION}", "--no-audit", "--no-fund"],
            capture_output=True, stdin=subprocess.DEVNULL, env=_environment(), timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise DeploymentError("배포 도구 설치에 실패했습니다. 인터넷 연결을 확인하고 다시 시도하세요.")
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError("배포 도구 설치를 완료하지 못했습니다. 인터넷 연결을 확인하세요.") from None
    vercel_command()
    return {"installed": True}


def _pages(args: list[str], field: str, *, cwd: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    for _ in range(100):
        page = _json([*args, "--limit", "100", *(["--next", cursor] if cursor else [])], cwd=cwd)
        values = page.get(field)
        if not isinstance(values, list):
            raise DeploymentError("Vercel 목록 형식이 달라졌습니다. 배포 도구를 업데이트하세요.", code="cli_outdated")
        items.extend(item for item in values if isinstance(item, dict))
        cursor = str((page.get("pagination") or {}).get("next") or "")
        if not cursor:
            return items
        if cursor in seen:
            break
        seen.add(cursor)
    raise DeploymentError("Vercel 목록을 끝까지 읽지 못했습니다. 다시 조회하세요.")


def load_account(cwd: Path, progress: Progress) -> dict[str, Any]:
    progress("Vercel 로그인과 배포 가능한 팀 확인 중")
    account = _json(["whoami"], cwd=cwd)
    teams = _pages(["teams", "ls"], "teams", cwd=cwd)
    scopes = [{"slug": team["slug"], "name": team.get("name") or team["slug"]}
              for team in teams if team.get("slug")]
    if not scopes and account.get("username"):
        scopes = [{"slug": account["username"], "name": account["username"]}]
    if not account.get("username") or not scopes:
        raise DeploymentError("배포 가능한 Vercel 계정·팀을 찾지 못했습니다. 계정 권한을 확인하세요.")
    return {"username": account["username"], "scopes": scopes}


def login(cwd: Path, progress: Progress) -> dict[str, Any]:
    progress("열리는 Vercel 브라우저에서 로그인·승인을 마치세요. 최대 5분 기다립니다.")
    try:
        process = subprocess.Popen(
            [*vercel_command(), "login", "--no-color"], cwd=cwd, env=_environment(cwd),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise DeploymentError("Vercel 로그인을 시작하지 못했습니다. 배포 도구를 업데이트하세요.") from None
    timer = threading.Timer(300, process.kill)
    timer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            # Only the official device-auth link is allowed into the UI; never raw logs.
            match = re.search(r"https://vercel\.com/oauth/device\?user_code=[A-Za-z0-9-]+", line)
            if match:
                progress("login_url:" + match.group(0))
        if process.wait() != 0:
            raise DeploymentError("로그인이 완료되지 않았습니다. 다시 로그인하여 브라우저에서 승인하세요.")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
    return load_account(cwd, progress)


def _slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", value) or "---" in value:
        raise DeploymentError("프로젝트·팀 이름은 영문 소문자, 숫자, 하이픈과 밑줄로 입력하세요(100자 이하).")
    return value


def load_projects(cwd: Path, scope: str, progress: Progress) -> dict[str, Any]:
    progress("선택한 팀의 Vercel 프로젝트 확인 중")
    projects = _pages(["project", "ls", "--scope", _slug(scope)], "projects", cwd=cwd)
    return {"scope": scope, "projects": [
        {"id": item["id"], "name": item["name"]} for item in projects if item.get("id") and item.get("name")
    ]}


def connect_account(cwd: Path, preferred_scope: str, progress: Progress) -> dict[str, Any]:
    """Reuse CLI login or open device login; load a known/single team's projects."""
    try:
        account = load_account(cwd, progress)
    except DeploymentError as exc:
        if exc.code != "login_required":
            raise
        account = login(cwd, progress)
    scopes = [item["slug"] for item in account["scopes"]]
    scope = preferred_scope if preferred_scope in scopes else scopes[0] if len(scopes) == 1 else ""
    return {"account": account,
            "projects": load_projects(cwd, scope, progress) if scope else {}}


def deployment_identity(runtime: Path) -> str:
    manifest = runtime / "mcp_runtime_manifest.json"
    return hashlib.sha256(str(runtime.resolve()).encode() + manifest.read_bytes()).hexdigest()


def _receipt_path(runtime: Path) -> Path:
    return runtime.parent.parent / ".vercel-deployments" / (deployment_identity(runtime) + ".json")


def read_deployment(runtime: Path) -> dict[str, Any]:
    try:
        data = json.loads(_receipt_path(runtime).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("identity") == deployment_identity(runtime):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _target_identity(runtime: Path) -> str:
    manifest = json.loads((runtime / "mcp_runtime_manifest.json").read_text(encoding="utf-8"))
    # Keep the destination when this bundle is regenerated, while isolating
    # institutions that happen to reuse the same output folder.
    scope = [str(runtime.resolve()), manifest.get("tenant_id"), manifest.get("profile_id")]
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()


def read_previous_deployment(runtime: Path) -> dict[str, Any]:
    """Return a destination hint, never evidence that changed data is deployed."""
    try:
        target_identity = _target_identity(runtime)
        path = runtime.parent.parent / ".vercel-deployments" / f"target-{target_identity}.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(result, dict) and result.get("target_identity") == target_identity:
            _slug(str(result.get("scope") or ""))
            _slug(str(result.get("project") or ""))
            return result
    except (OSError, ValueError, TypeError, DeploymentError):
        pass
    return read_deployment(runtime)


def _save_deployment(runtime: Path, result: dict[str, Any]) -> None:
    if result.get("identity") != deployment_identity(runtime):
        raise DeploymentError("규정 묶음이 변경됐습니다. 새 묶음을 배포한 뒤 연결을 검사하세요.", code="stale_bundle")
    path = _receipt_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    if result.get("scope") and result.get("project"):
        target_identity = _target_identity(runtime)
        target = path.parent / f"target-{target_identity}.json"
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps({**result, "target_identity": target_identity},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)


def production_mcp_url(inspection: dict[str, Any]) -> str:
    if inspection.get("readyState", inspection.get("status")) != "READY" or inspection.get("target") != "production":
        raise DeploymentError("Production 배포가 아직 Ready 상태가 아닙니다. Vercel 배포 상태를 확인하세요.")
    aliases = inspection.get("alias") or inspection.get("aliases") or []
    for alias in aliases:
        host = alias.get("alias") if isinstance(alias, dict) else alias
        if not isinstance(host, str):
            continue
        parsed = urlsplit(host if "://" in host else "https://" + host)
        if (parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".vercel.app")
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in {"", "/"} and parsed.port is None):
            return f"https://{parsed.hostname}/mcp"
    raise DeploymentError("고정 Production 주소를 찾지 못했습니다. Vercel의 Domains 설정을 확인하세요.")


def verify_deployment(runtime: Path, result: dict[str, Any], progress: Progress) -> dict[str, Any]:
    if result.get("identity") != deployment_identity(runtime):
        raise DeploymentError("규정 묶음이 변경됐습니다. 새 묶음을 배포한 뒤 연결을 검사하세요.", code="stale_bundle")
    if not result.get("url"):
        # A successful upload is recoverable even when inspection timed out or
        # the app was closed before the stable Production alias was confirmed.
        # Never present the deployment-specific URL as an AI connection address.
        progress("Production 완료 상태와 고정 주소 확인 중")
        inspection = _json(["inspect", result["deployment_url"], "--scope", _slug(result["scope"])],
                           cwd=runtime.parent)
        result = {**result, "url": production_mcp_url(inspection), "status": "ready"}
        _save_deployment(runtime, result)
    progress("배포 완료 · 원격 MCP 검색과 본문 조회 검사 중")
    report = run_mcp_client_config_smoke(
        server_name=result["server_name"], remote_url=result["url"],
        allow_unauthenticated_remote=True, timeout_seconds=90,
    )
    checks = {key: report.get(key) is True for key in (
        "mcp_initialized", "tools_discovered", "end_to_end_verified",
    )}
    diagnosis = "passed"
    if not checks["mcp_initialized"]:
        errors = str(report.get("results", [])).casefold()
        diagnosis = "access" if any(word in errors for word in ("401", "403", "unauthorized", "forbidden")) else "connection"
    elif not checks["tools_discovered"]:
        diagnosis = "tools"
    elif not checks["end_to_end_verified"]:
        diagnosis = "search_fetch"
    result = {**result, "checks": checks, "verified": all(checks.values()),
              "diagnosis": diagnosis, "checked_at": time.time()}
    _save_deployment(runtime, result)
    return result


def deploy_public_runtime(
    *, runtime: Path, server_name: str, scope: str, project: str,
    public_approved: bool, replace_existing: bool, progress: Progress,
) -> dict[str, Any]:
    """Publish only a sealed approved export after the operator's explicit action."""
    if not public_approved:
        raise DeploymentError("선택한 승인 규정의 인터넷 공개 허용 여부를 먼저 확인하세요.")
    scope, project = _slug(scope), _slug(project)
    if not _DEPLOY_LOCK.acquire(blocking=False):
        raise DeploymentError("다른 Vercel 배포가 진행 중입니다. 완료 후 다시 시도하세요.")
    try:
        runtime = runtime.resolve()
        identity = deployment_identity(runtime)
        progress("승인 데이터와 파일 무결성 검사 중")
        manifest = validate_mcp_runtime_data_bundle_integrity(runtime)
        # Re-read remote state immediately before mutation, not just the UI's cached list.
        existing = load_projects(runtime.parent, scope, progress)["projects"]
        exists = any(item["name"] == project for item in existing)
        if exists != replace_existing:
            raise DeploymentError("프로젝트 목록이 바뀌었습니다. 목록을 새로 불러와 새 배포 또는 업데이트 대상을 확인하세요.")
        stage = runtime.parent.parent / ("vercel-mcp-stage-" + uuid.uuid4().hex[:12])
        progress("승인된 규정만 배포 폴더로 준비 중")
        prepare_vercel_mcp_deployment(runtime_data_dir=runtime, out_dir=stage,
                                      compress_runtime_file_under_bytes=0, verify_sealed_runtime=True)
        if deployment_identity(runtime) != identity:
            raise DeploymentError("작업 중 MCP 묶음이 변경되었습니다. 새 묶음으로 다시 배포하세요.")
        progress("선택한 Vercel 프로젝트 연결 중")
        _run(["link", "--yes", "--project", project, "--scope", scope], cwd=stage)
        progress("Vercel Production 업로드·빌드 중 — 몇 분 걸릴 수 있습니다.")
        # Deployment-scoped public values cannot weaken other deployments' environment.
        output = _run([
            "deploy", "--prod", "--yes", "--archive=tgz", "--scope", scope,
            "--env", "MCP_ALLOW_UNAUTHENTICATED_HTTP=true",
            "--env", "MCP_TOOL_PROFILE=chatgpt-data",
            "--env", "MCP_AUTH_TOKEN=",
            "--env", f"MCP_TENANT_ID={manifest['tenant_id']}",
            "--env", f"MCP_PROFILE_ID={manifest.get('profile_id') or ''}",
        ], cwd=stage, timeout=900)
        urls = re.findall(r"https://[a-zA-Z0-9.-]+\.vercel\.app", output)
        if not urls:
            raise DeploymentError("배포 응답에서 주소를 찾지 못했습니다. Vercel 대시보드에서 배포 결과를 확인하세요.")
        result = {"identity": identity, "server_name": server_name, "scope": scope,
                  "project": project, "deployment_url": urls[-1], "status": "pending",
                  "verified": False, "deployed_at": time.time()}
        # Save the remote upload identity before the next network operation, so
        # a retry/restart only inspects and tests it instead of publishing again.
        _save_deployment(runtime, result)
        return verify_deployment(runtime, result, progress)
    except DeploymentError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise DeploymentError("승인 묶음 검증 또는 배포 결과 저장에 실패했습니다. 묶음을 확인하고 다시 시도하세요.") from None
    finally:
        _DEPLOY_LOCK.release()


class DeploymentJob:
    """Rerun-safe session job: workers never access Streamlit APIs."""

    def __init__(self, operation: Callable[[Progress], dict[str, Any]]) -> None:
        self.message = "작업 시작 중"
        self.login_url = ""
        self.result: dict[str, Any] | None = None
        self.error = ""
        self.error_code = ""
        self.started = time.monotonic()
        self.started_at = time.time()
        self.done = threading.Event()

        def report(message: str) -> None:
            if message.startswith("login_url:"):
                self.login_url = message.removeprefix("login_url:")
            else:
                self.message = message

        def work() -> None:
            try:
                self.result = operation(report)
            except DeploymentError as exc:
                self.error = str(exc)
                self.error_code = exc.code
            except Exception:
                self.error = "작업을 완료하지 못했습니다. 입력값과 배포 상태를 확인한 뒤 다시 시도하세요."
            finally:
                self.done.set()

        threading.Thread(target=work, name="vercel-operator-job", daemon=True).start()
