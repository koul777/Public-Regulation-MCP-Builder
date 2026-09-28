# 초보자 화면 안내 실행 검증

검증일: 2026-09-26. Windows, Python 3.13.3, Streamlit 1.64.0, Node.js 24.14.0에서 저장소의 가상환경으로 실행했다. 이 문서는 초보자 안내와 실제 화면 이동에 대한 집중 검증 기록이며, 전체 릴리스 승인 기록은 아니다.

## 실행 결과

저장소 루트에서 실행한다.

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_beginner_tour tests.test_streamlit_beginner_guide -v
```

64개 테스트가 72.492초에 통과했다. 기존 54개 테스트의 승인·색인 조건을 유지하면서 안내 모듈 테스트 7개와 실제 Streamlit AppTest 시나리오 3개를 추가했다. 건너뛴 테스트는 없었다.

그 후 실제 저장된 청크가 미승인 상태인지와 승인 저널이 비어 있는지를 추가로 단언하고 해당 시나리오를 다시 실행했다.

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_streamlit_beginner_guide.StreamlitBeginnerJourneyExecutionTests.test_synthetic_docx_processing_moves_to_review_without_automatic_approval -v
```

1개 테스트가 10.952초에 통과했다. AppTest 실행에서 `missing ScriptRunContext` 등 테스트 환경 경고가 출력됐지만 앱 예외와 테스트 실패는 없었다.

추가 회귀 검증으로 첫 문서는 승인·색인 완료, 둘째 문서는 미완료인 선택 묶음을 검사했다.

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_streamlit_beginner_guide.StreamlitBeginnerJourneyExecutionTests.test_completed_current_document_opens_next_pending_document_before_ai_handoff -v
```

1개 테스트가 7.185초에 통과했다. 현재 문서만 내보내도록 설정해도 선택한 묶음의 미완료 규정을 무시하지 않았고, 다음 미완료 규정 버튼으로 두 번째 문서가 열렸다. 이 추가 후 초보자 안내 테스트 두 모듈의 테스트 수는 65개다.

최신 AppTest 프록시에서 제거된 `filtered_state` 직접 접근 5곳을 공개 멤버십 검사로 바꾸고, 해당 접근을 사용하던 승인 화면 테스트 4개를 재실행했다. 승인 조건·검수 확인·기존 작업 보존에 관한 단언은 유지했다.

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest `
  tests.test_streamlit_approval_app.StreamlitApprovalAppTests.test_approval_tabs_smoke_reflect_human_check_and_approve `
  tests.test_streamlit_approval_app.StreamlitApprovalAppTests.test_paginated_approval_keeps_one_click_override_for_unseen_row `
  tests.test_streamlit_approval_app.StreamlitApprovalAppTests.test_primary_next_button_uses_transition_dialog_then_changes_page `
  tests.test_streamlit_approval_app.StreamlitApprovalAppTests.test_remaining_review_buttons_preserve_completed_work_and_fill_only_missing_items -v
```

4개 테스트가 22.247초에 통과했다.

## 확인한 동작

| 대상 | 실제 검증 방법 | 확인 결과 |
| --- | --- | --- |
| 안내 데이터 이스케이프 | 따옴표, HTML 태그, 스크립트 닫기 태그, 한글을 마커·설정에 넣고 HTML과 JSON을 다시 해석 | 추가 태그·이벤트 속성 삽입 없이 원래 문자열 유지 |
| 새·구 Streamlit 렌더 경로 | `st.html`의 스크립트 지원 유무를 각각 모사 | 지원 시 호스트 HTML, 미지원 시 높이 0의 iframe 선택; 세션의 승인·색인 값 불변 |
| 안내 비활성화 | 실제 JavaScript 파일을 Node VM에서 실행하고 DOM·저장소·네트워크 접근 차단 | 이전 안내 컨트롤러를 한 번 정리한 뒤 종료 |
| 실제 진행률 | 미완료 상태에서 마지막 화면 방문, 전 단계 완료, 선택적 결과 단계 제외를 각각 렌더 | 방문을 완료로 계산하지 않음; 생략된 단계는 분모와 완료 수에서 제외 |
| 기관 생성·안내 전환 | AppTest에서 합성 기관 등록, 안내 끄기·켜기, 다시 보기 실행 | 전처리 화면 진입, 기관 선택 유지, 안내 재요청 갱신; 저장 파일과 문서 생성 상태 불변 |
| 전처리 준비 조건 | 합성 DOCX를 대기 파일로 넣고 화면에서 선택 | 규정 정보 확인 전 시작 버튼 비활성화, 직접 확인한 뒤 활성화 |
| 실제 전처리·다음 화면 | AppTest에서 전처리 실행 후 다음 버튼 클릭 | 문서 상태 `completed`, 청크 생성, 미승인 상태 유지; AI 추가 검수 미사용 시 승인 화면으로 바로 이동 |
| 승인 전 최종 단계 이동 | 승인 화면에서 최종 사용 메뉴 선택 후 저장 파일 전후 비교 | 미완료 검수 화면으로 돌아옴; 승인 저널과 벡터 색인 생성 없음 |
| 여러 문서의 다음 검수 | 첫 문서만 버튼으로 승인·색인하고 둘째 문서는 미완료로 유지 | 현재 문서 완료와 전체 선택 범위 완료 구분; AI 연결·안내 다음 단계 비활성화, 다음 미완료 문서는 정상 진입 |
| 보호 모드 | 인증 필수 또는 테넌트 저장소 분리 상태에서 초보자 안내 활성화 | 운영자 UI 차단 유지; 실행 버튼·iframe·런타임 파일 생성 없음 |
| 자산 제공 | 패키지 리소스 읽기와 `pyproject.toml`의 package-data 선언 확인 | 로컬 JavaScript·CSS 두 파일 존재 및 wheel 포함 선언 확인 |

전처리 후 승인 화면에서는 결과 확인 단계가 제외된 `1 / 3 완료` 표시와 기본 메뉴 구성을 검증했다. 기존 테스트는 실제 승인·색인 게이트, 문서 및 MCP 묶음 변경 시 확인 무효화, 외부 연결 확인 순서, 기관 전환 시 승인 컨텍스트 정리도 계속 검사한다.

## 합성 데이터와 실행 격리

추가한 AppTest는 `build_synthetic_regulation_docx()`가 만드는 공개 합성 문서와 테스트용 기관명만 사용한다. 기관 등록 파일, 업로드, 처리 결과, 저널은 테스트별 임시 디렉터리에 저장하고 종료 시 정리한다. 전역 런타임 설정도 테스트 종료 시 복원한다.

외부 AI 검수, 로컬 구조 검수, OCR, Kordoc 실행을 끄고 API 키를 빈 값으로 설정한다. 일반 소켓 연결은 테스트에서 실패하도록 막는다. Windows asyncio가 이벤트 루프 내부 통신을 위해 만드는 `socketpair()` 연결만 허용하며, 로컬 모델 서버 연결도 허용하지 않는다. 실제 기관 문서나 기존 운영 데이터는 사용하지 않는다.

여러 문서 회귀 테스트는 기존 공개 합성 문서·청크·처리 메타데이터 픽스처를 사용한다. 승인 저장과 로컬 색인은 실제 코드를 실행하되, 임베딩 모델 어댑터만 항상 같은 384차원 벡터를 돌려주는 테스트 대역으로 바꾼다. 이 결과는 모델이나 Kordoc의 실제 품질 검증을 의미하지 않는다.

## 검증 한계와 남은 확인

- AppTest는 Streamlit의 Python 실행과 위젯 상태 전이를 검증한다. 브라우저의 JavaScript 실행, 안내 카드 배치, 모바일 화면, 스크롤, 키보드 초점, 애니메이션은 이 결과만으로 입증되지 않는다. 실제 브라우저 검증은 별도 증거가 필요하다.
- Node VM 테스트는 비활성화 경로만 실행한다. 활성 안내의 자동 표시·닫기·다시 열기·화면 재실행 시 정리는 브라우저에서 확인해야 한다. Node.js가 없는 환경에서는 해당 테스트가 명시적으로 건너뛰어지므로 결과의 skip 수를 확인한다.
- 두 Streamlit 렌더 분기는 모사된 함수 시그니처로 검사했다. Streamlit 구버전 전체를 설치해 확인한 호환성 결과는 아니다.
- 자산 테스트는 소스 리소스와 패키지 선언을 확인했다. 빌드된 sdist·wheel의 내용과 새 환경 설치 결과는 별도 빌드 검증 대상이다.
- 실제 사용자 대상 사용성 평가, 실제 기관 파일의 파싱 품질, 최종 승인 이후 MCP 앱 연결, 모델 응답 품질은 이 집중 검증 범위에 포함하지 않았다. 안내의 완료 표시가 이러한 검증이나 사람의 승인을 대신하지 않는다.
- 전체 테스트 스위트, 패키지 빌드, 공개 릴리스 위생 감사 결과는 담당 통합 검증에서 별도로 기록한다.

증거 코드는 [안내 모듈 테스트](../tests/test_beginner_tour.py), [Streamlit 초보자 안내 테스트](../tests/test_streamlit_beginner_guide.py), [승인 화면 테스트](../tests/test_streamlit_approval_app.py)에 있다.
