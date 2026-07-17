# Public Repository History Policy

이 문서는 현재 파일 트리의 공개 안전성 검사와 Git 전체 히스토리의 공개 안전성을 구분한다.

## 핵심 원칙

- 현재 `public-release` 트리가 audit를 통과해도 과거 private 커밋이 자동으로 안전해지는 것은 아니다.
- 기존 private 저장소의 visibility를 바로 `Public`으로 바꾸지 않는다.
- 과거 커밋, branch, tag, remote ref에 원본 문서, 기관 데이터, runtime export, secret, 내부 문서가 남아 있으면 공개하지 않는다.

## 권장 공개 경로

기존 private 저장소는 계속 private로 보존하고, `public-release`의 검증된 현재 트리만 사용해 별도 public 저장소를 만든다. 별도 저장소는 orphan commit으로 시작해야 하며, private 저장소의 과거 커밋을 parent로 상속하지 않는다.

권장 순서:

1. `public-release`의 public release gate와 fresh-clone 검증을 통과시킨다.
2. `scripts/create_public_orphan_snapshot.py`로 검증된 커밋의 tracked tree만 새 폴더에 내보낸다. 작업 폴더의 미추적 파일과 기존 `.git` 히스토리는 복사하지 않는다.
3. orphan snapshot에서 `git log --all --stat`, path/secret/identifier scan, fresh clone 검증을 다시 실행한다.
4. owner가 새 public 저장소의 이름, remote, branch 범위를 확인한다.
5. owner가 직접 새 public 저장소에 push하고 visibility를 확인한다.

예시:

```powershell
$publicSnapshotDir = Join-Path $env:TEMP "reg-rag-preprocessor-public"
python scripts\run_public_release_gate.py --root . --include-untracked --fail-on-blocked
python scripts\create_public_orphan_snapshot.py `
  --source-ref public-release `
  --output-dir $publicSnapshotDir
```

생성된 폴더에는 remote가 설정되지 않으며 자동 push도 수행하지 않는다. 새 저장소를 만들기 전 다음을 확인한다.

```powershell
Set-Location $publicSnapshotDir
git log --oneline --all
git remote -v
python scripts\run_public_release_gate.py --root . --include-untracked --fail-on-blocked
python -m unittest discover -s tests -v
```

`git log --oneline --all`에는 부모가 없는 단일 공개 커밋만 보여야 하고, `git remote -v`는 비어 있어야 한다. 검증 후에만 별도의 새 public 저장소 remote를 연결한다.

## 대체 경로

기존 저장소를 public으로 유지해야 한다면 모든 공개 branch와 tag의 히스토리를 전면 재작성하고, private ref 삭제 및 민감 blob의 비도달 상태를 별도로 검증해야 한다. 이 작업은 owner가 보존 정책과 백업을 확인한 후 수행한다.

## 현재 확인 결과

공개용 트리는 source-only audit를 통과하더라도 이전 private 커밋에 규정 원본이나 기관 데이터가 남아 있을 수 있다. 따라서 기존 private 저장소의 단순 visibility 전환은 승인하지 않고, 검증된 tracked tree만 부모 없는 새 공개 커밋으로 배포한다.
