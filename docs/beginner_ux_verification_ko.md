# 초보자 화면 안내 실행 검증

## 2026-09-28 추가 검증 — 실제 클릭으로 이어지는 안내

이번 보완은 밝게 표시된 실제 항목을 직접 누르면 다음 미완료 항목으로 안내가 이어지는
흐름을 대상으로 한다. 안내창을 넘기거나 닫는 것만으로 승인·색인 상태를 바꾸지 않는다.

- Streamlit의 HTML 정제 경로에서 스크립트가 제거되는 실제 재현을 확인했다.
  지원되는 버전은 높이 1px의 `st.iframe`, 이전 버전은 높이 0의 HTML 컴포넌트로 저장소에 포함된
  안내 코드만 실행한다. 문서 내용이나 AI 응답을 실행 코드에 넣지 않는다.
- 현재 절차와 전처리 완료 후 이동 버튼의 우선순위를 사용해 지금 누를 항목을 고른다.
  접힌 메뉴 안의 항목은 먼저 메뉴 제목을 가리킨다.
- 화면이 다시 실행돼도 진행 중인 안내와 일시정지·자동 안내 중지 설정이 유지된다.
  안내의 설명을 넘겨도 작업 완료를 만들지 않으며, 앱 확인창이 열리면 안내가 물러난다.
- 표나 위젯이 늦게 렌더링되어 배치가 바뀌는 경우에도 강조 테두리 위치를 다시 계산한다.
- 독립 Qwen 화면은 기관·규정을 명시적으로 선택한 후 연결 확인·질문·근거 확인을 안내한다.
  연결 실패, 오류 답변, 인용 없는 답변을 근거 확인 단계로 올리지 않는다.

### 확인 방법과 범위

| 확인 | 방법 | 범위 |
| --- | --- | --- |
| 안내창의 실제 클릭 동작 | `tests/browser/beginner-tour.cjs`를 Chromium에서 실행 | 다음 항목 전환, 같은 항목 반복 방지, 우선순위, 접힌 메뉴, 앱 확인창, 멈춤·재개, 자동 안내 중지, 재실행 정리, 배치 이동 |
| 키보드·작은 화면 | 같은 브라우저 검사 | Tab, Esc, 390px 화면 경계와 가로 넘침, 모션 줄이기 |
| 실제 빌더 화면 | 격리된 로컬 Streamlit 앱과 공개 합성 DOCX | 시작 안내, 기관명 입력·기관 생성, 업로드, 인식 정보 확인, 전처리, 검수 화면 이동, 항목별 판단·검수 확인·원문 대조·다음 조항, 안내 멈춤·재개 |
| Qwen의 단계 연결 | `tests.test_qwen_chat_app`의 Streamlit AppTest | 기관·규정 선택, 연결 확인 전 질문 차단, 연속 질문 2회, 실제 인용이 있는 마지막 답변 안내 |
| 실행 상태 격리 | Qwen 화면 테스트 뒤 저장소·저널 테스트 100개 실행 | AppTest가 교체한 `__main__` 복원 후 Windows 자식 프로세스 회귀 통과 |
| Kordoc 패치 | 설치 기준 4.15.7 및 실제 실행 파일 확인 | 공개 합성 DOCX의 2×2 표 1개 추출, 표 안의 한글 내용 유지 |

브라우저 회귀 검사는 별도 Playwright 설치가 필요하다. Python 제품 의존성에는 추가하지
않는다. 설치된 Playwright가 Node.js에서 보이는 환경에서 다음 명령을 실행한다.

```powershell
node tests/browser/beginner-tour.cjs
python -m unittest tests.test_beginner_tour tests.test_streamlit_beginner_guide tests.test_qwen_chat_app -v
```

Qwen 연결·답변은 이 테스트에서 합성 응답으로 대체한다. 실제 모델 답변 품질, 외부 AI 앱의
설정 화면, 사람 5명 사용성 파일럿은 이 검증의 범위가 아니다. 실제 기관 원문, 전달받은
참고 영상, 테스트용 런타임 데이터는 공개 저장소에 포함하지 않는다.

### README 시연 재현

README의 GIF·MP4는 실제 빌더 화면을 Chromium에서 녹화한 것이다. 주황색 원은
녹화용 마우스 포인터이며 제품 기능은 아니다. 합성 기관과 합성 DOCX만 사용하고,
외부 AI 검수 및 Kordoc 호출은 이 UI 시연에서 끈다. 최종 승인·색인이나 모델 답변까지
수행한 영상으로 해석하지 않는다. Kordoc 4.15.7 실행 검증은 별도로 수행했다.

첫 터미널에서 격리된 앱을 실행한다. 실제 사용자 저장소 대신 `tmp/guide-demo-*`를 사용한다.

```powershell
python -m streamlit run tests/browser/beginner_demo_app.py --server.address 127.0.0.1 --server.port 8766 --server.headless true --browser.gatherUsageStats false
```

Playwright와 Chromium이 설치된 다른 터미널에서 녹화한다.

```powershell
node scripts/capture_beginner_click_demo.cjs
ffmpeg -y -ss 2 -i tmp/beginner-click-recording/beginner-click-guide.webm -c:v libx264 -crf 23 -pix_fmt yuv420p -movflags +faststart -an docs/assets/beginner-click-guide.mp4
ffmpeg -y -i docs/assets/beginner-click-guide.mp4 -filter_complex "fps=5,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" -loop 0 docs/assets/beginner-click-guide.gif
```

파일 선택은 브라우저 업로드 입력에 합성 파일을 전달한다. Windows 파일 선택창은 녹화하지
않는다. 기관 생성, 전처리와 검수 확인은 실제 화면의 컨트롤을 클릭한다. 녹화 원본과
런타임은 `tmp/`에만 남기며 공개하는 파일은 검사한 GIF·MP4·PNG 세 개다.

아래는 앞선 검증 시점의 기록이다.

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
