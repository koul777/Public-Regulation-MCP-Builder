# Third-Party Notices

이 문서는 이 프로젝트가 명시적으로 연동하거나 `pyproject.toml`에 직접 선언한 제3자 소프트웨어의 라이선스 확인 상태를 기록하는 초안이다. 각 구성요소의 라이선스는 프로젝트 자체 라이선스와 별도로 적용된다.

## 확인 범위와 상태 기준

- 런타임 직접 의존성은 `pyproject.toml`의 `[project].dependencies`를 기준으로 했다.
- 아래 버전은 2026-07-15 현재 작업 환경에 설치된 distribution의 스냅샷이다. `pyproject.toml`의 최소 버전 범위나 lockfile을 의미하지 않는다.
- `metadata + bundled file`: 설치된 distribution metadata의 라이선스 표현과 배포판 내부 license file을 함께 확인했다.
- `metadata only`: metadata의 라이선스 표현은 확인했지만 현재 설치 위치에서 license file을 찾지 못했다. release artifact에서 재확인해야 한다.
- `partial / TODO`: 라이선스 계열은 확인했지만 SPDX 식별자나 정확한 변형을 확정할 정보가 부족하다. 추측하지 않는다.
- 이 표는 직접 의존성 중심이며 transitive dependency, optional extra의 전체 closure, 운영 이미지에 포함되는 시스템 패키지를 대체하지 않는다.

## Runtime direct dependencies

| Package | Declared requirement | Installed snapshot | License evidence | 확인 상태 | Upstream / source |
| --- | --- | --- | --- | --- | --- |
| FastAPI | `fastapi>=0.111` | 0.135.2 | `License-Expression: MIT`; `fastapi-0.135.2.dist-info/licenses/LICENSE` | metadata + bundled file | <https://github.com/fastapi/fastapi> |
| Uvicorn | `uvicorn[standard]>=0.30` | 0.42.0 | `License-Expression: BSD-3-Clause`; `uvicorn-0.42.0.dist-info/licenses/LICENSE.md` | metadata + bundled file | <https://github.com/Kludex/uvicorn> |
| Pydantic | `pydantic>=2.0` | 2.11.7 | `License-Expression: MIT`; `pydantic-2.11.7.dist-info/licenses/LICENSE` | metadata + bundled file | <https://github.com/pydantic/pydantic> |
| python-multipart | `python-multipart>=0.0.9` | 0.0.22 | `License-Expression: Apache-2.0`; `python_multipart-0.0.22.dist-info/licenses/LICENSE.txt` | metadata + bundled file | <https://github.com/Kludex/python-multipart> |
| Streamlit | `streamlit>=1.35` | 1.50.0 | `License: Apache License 2.0`; Apache license classifier | metadata only; release artifact license file 확인 TODO | <https://github.com/streamlit/streamlit> |
| pandas | `pandas>=2.0` | 2.2.2 | `License: BSD 3-Clause License`; BSD license classifier; `pandas-2.2.2.dist-info/LICENSE` | metadata + bundled file | <https://github.com/pandas-dev/pandas> |
| PyMuPDF | `pymupdf>=1.24` | 1.27.1 | `License: Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`; `pymupdf-1.27.1.dist-info/COPYING` | 확인됨; 배포·호스팅 모델별 legal review 필요 | <https://github.com/pymupdf/pymupdf> |
| python-docx | `python-docx>=1.1` | 1.2.0 | `License: MIT`; MIT license classifier; `python_docx-1.2.0.dist-info/licenses/LICENSE` | metadata + bundled file | <https://github.com/python-openxml/python-docx> |
| olefile | `olefile>=0.47` | 0.47 | `License: BSD`; BSD license classifier; `olefile-0.47.dist-info/LICENSE.txt`에 BSD 조건과 PIL 유래 고지 포함 | partial; 복합 고지의 정확한 SPDX 조합 확인 TODO | <https://www.decalage.info/python/olefileio> |
| MCP Python SDK | `mcp>=1.26` | 1.26.0 | `License: MIT`; MIT license classifier; `mcp-1.26.0.dist-info/licenses/LICENSE` | metadata + bundled file | <https://github.com/modelcontextprotocol/python-sdk> |
| kiwipiepy | `kiwipiepy>=0.21` | 0.23.2 | `License: LGPL v3 License`; LGPLv3 classifier; `kiwipiepy-0.23.2.dist-info/licenses/LICENSE.txt` | 확인됨; 배포 시 LGPL 조건 review 필요 | <https://github.com/bab2min/kiwipiepy> |

### PyMuPDF 별도 주의

PyMuPDF의 현재 설치 metadata는 AGPL-3.0 또는 Artifex Commercial License의 이중 라이선스로 표시된다. 이 프로젝트의 MIT·Apache-2.0·AGPL-3.0 후보 중 하나를 고르는 것만으로 PyMuPDF의 선택과 의무가 자동으로 정해지지 않는다.

owner 또는 법무 검토가 필요한 선택지는 다음과 같다.

- PyMuPDF를 AGPL 조건으로 사용하고 소스·배포·네트워크 서비스 의무를 수용할지
- Artifex 상용 라이선스를 별도로 계약할지
- PyMuPDF를 선택적 기능 또는 다른 라이브러리로 대체할지
- wheel, container, 기관 내부 서비스 중 어떤 형태로 배포할지

이 항목은 프로젝트 라이선스 선택과 별도의 blocker로 기록해야 한다.

### kiwipiepy 별도 주의

kiwipiepy는 LGPLv3로 표시된다. 소스 저장소에 직접 코드를 복사하지 않았더라도 wheel·container 또는 수정된 라이브러리를 배포하는 경우 LGPL 고지, 라이선스 전문, 수정 여부와 재링크 관련 조건을 배포 모델에 맞춰 확인해야 한다. 최종 배포물에 대한 법무 검토는 TODO이다.

## Optional development dependencies

| Package | Declared requirement | Installed snapshot | License evidence | 확인 상태 |
| --- | --- | --- | --- | --- |
| build | `build>=1.2` | 1.4.0 | `License-Expression: MIT` | metadata 확인 |
| pytest | `pytest>=8.0` | 7.4.4 | `License: MIT`; MIT license classifier | metadata 확인; 설치 버전은 선언 범위 미충족, release 환경 재현 TODO |

개발 의존성은 runtime 배포물에 항상 포함된다고 가정하지 않는다. 배포 이미지나 개발자용 설치 문서에 포함할 경우 해당 범위를 다시 확인해야 한다.

## Kordoc

Kordoc은 `pyproject.toml`의 Python 직접 의존성이 아니라, 사용자가 별도로 설치할 수 있는 외부 CLI이다. 현재 저장소 문서에 기록된 연동 범위와 라이선스 고지를 유지한다.

- Project: <https://github.com/chrisryugj/kordoc>
- Usage: HWP, HWPX, PDF, DOCX 문서의 표 구조 추출용 외부 CLI
- Integration: 사용자가 별도로 설치한 실행 파일을 subprocess로 호출
- Bundled in this repository or release package: No, 현재 문서 기준
- Upstream source copied or modified here: No, 현재 문서 기준
- License: MIT License, upstream `LICENSE`를 release 시점에 재확인할 것
- Copyright: Copyright (c) 2026 chrisryugj, 현재 저장소 문서 기록 기준
- Upstream license: <https://github.com/chrisryugj/kordoc/blob/main/LICENSE>

Kordoc 소스나 바이너리를 복사하거나 배포물에 포함하게 되면, 해당 사본 또는 소프트웨어의 상당 부분에 upstream 저작권 및 MIT 허가 고지를 함께 포함해야 한다. 현재는 외부 실행 파일을 별도로 설치해 호출하는 범위로만 기록한다.

### MIT License text for Kordoc

```text
MIT License

Copyright (c) 2026 chrisryugj

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 남은 확인 TODO

1. release 환경의 정확한 Python 버전과 lock/export 방식을 정하고 transitive dependency 전체를 생성한다.
2. `uvicorn[standard]` extra가 끌어오는 패키지와 운영 이미지의 시스템 라이브러리 라이선스를 확인한다.
3. Streamlit 배포 artifact에서 Apache-2.0 전문 또는 upstream 고지 파일을 재확인한다.
4. olefile의 BSD 및 PIL 유래 복합 고지에 대한 정확한 SPDX 조합과 배포 고지 범위를 upstream 문서에서 확인한다.
5. PyMuPDF의 AGPL 또는 Artifex Commercial 선택, kiwipiepy의 배포 조건을 owner/legal 결정으로 기록한다.
6. 소스-only 공개 branch에 실제로 포함되는 파일과 wheel·sdist·container에 포함되는 파일별로 고지 범위를 확정한다.
7. owner가 프로젝트 라이선스를 결정한 뒤 `LICENSE`, project metadata, README, CONTRIBUTING 문서를 일관되게 갱신한다.
