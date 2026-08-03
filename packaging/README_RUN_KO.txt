PR MCP Builder Windows 실행판
공공기관 규정 MCP 빌더

1. ZIP 파일을 일반 폴더에 완전히 압축 해제합니다.
2. PR MCP Builder.exe를 더블클릭합니다.
3. Windows 보호 화면이 나오면 '추가 정보'를 누른 뒤 '실행'을 선택합니다.
4. 브라우저가 자동으로 열리면 화면의 1~4단계를 순서대로 진행합니다.

HWP/HWPX/PDF/DOCX 규정을 MCP까지 만들려면 Kordoc 준비가 필요합니다.
1. https://nodejs.org 에서 Node.js LTS를 설치합니다.
2. PR MCP Builder를 완전히 종료한 뒤 다시 실행합니다.
3. ① 화면에서 'Kordoc 설치·검증 시작'을 누르고, 완료 후 앱을 다시 시작합니다.
Node.js/npm과 Kordoc 설치는 화면에서 동의해 버튼을 누른 경우에만 진행합니다.

브라우저가 자동으로 열리지 않으면 함께 열린 콘솔 창을 닫지 말고,
그 창에 표시된 http://127.0.0.1:포트 주소를 복사해 브라우저 주소창에 붙여 넣으세요.

Python 설치는 필요하지 않습니다.
이 안내는 이 폴더의 PR MCP Builder.exe와 여기서 생성한 로컬 MCP 폴더를 같은 PC에서
사용할 때만 적용됩니다. 별도로 만든 '전달용 MCP ZIP'을 다른 PC에서 로컬 STDIO로
실행하려면 대상 PC에 Python 3.11 이상이 필요합니다. Python이 없는 대상 PC라면 이
portable 실행판 전체를 옮긴 뒤 그 PC에서 MCP 묶음을 다시 생성하고 AI 앱에 재등록하세요.
작업 데이터는 %LOCALAPPDATA%\PR MCP Builder\data에 저장됩니다.
프로그램을 종료하려면 함께 열린 콘솔 창을 닫습니다.
