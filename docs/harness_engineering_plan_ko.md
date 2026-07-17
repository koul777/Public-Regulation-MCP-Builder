# 공개 릴리스 하네스 운영 계획

PR MCP Builder의 공개 릴리스는 실제 기관 문서나 런타임 데이터를 포함하지 않은 상태에서 재현 가능한 자동 검증을 통과해야 합니다.

## 필수 검증

1. `python -m unittest discover -s tests -v`
2. `python -m build --sdist --wheel`
3. `python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan`

## 검증 범위

- 파서와 청크 구조의 결정적 동작
- 기관·테넌트 범위 격리
- 사람 승인 전 색인 차단
- 승인 청크와 MCP 노출 기록의 일치
- 배포물의 로컬 경로, 비밀정보, 원본 문서와 런타임 데이터 제외

검증 결과와 실행 명령은 pull request에 기록하고, 실패한 검사를 우회한 산출물은 공개 릴리스로 배포하지 않습니다.
