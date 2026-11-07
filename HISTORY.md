# Intel Arc GPU 기반 Qwen2.5-Coder-3B 로컬 서빙 및 코딩 에이전트 구축 일지

## 1. 하드웨어 환경 및 제약 분석
- **시스템 사양:** Windows 환경, Intel Arc A350M 외장 GPU (전용 VRAM 4GB / 작업 관리자 `GPU 0`) + 내장 그래픽 (공유 메모리 7.8GB / `GPU 1`) + 시스템 RAM 16GB
- **핵심 제약 및 발견:**
  - `GPU 1`의 7.8GB는 시스템 RAM을 빌려 쓰는 공유 메모리이므로 연산 속도가 느림.
  - AI 전용 가속 코어(XMX)와 고속 VRAM(4GB)을 탑재한 **`GPU 0` (Intel Arc A350M)**을 타겟팅해야 함.
  - `Qwen2.5-Coder-3B-Instruct` (4-bit 양자화 `Q4_K_M`, 약 2.2GB) 모델은 4GB VRAM 환경에서 구동 가능한 최적의 체급임.

---

## 2. 서빙 엔진 전환 및 트러블슈팅 히스토리

### 1단계: Ollama 초기 테스트 및 한계
- **시도:** 환경 변수(`ONEAPI_DEVICE_SELECTOR=level_zero:0`, `OLLAMA_VULKAN=1`)를 설정해 `qwen2.5-coder:1.5b` 및 `3b` 모델 구동.
- **한계:** 백그라운드 상주 메모리 소모가 크고, VRAM 4GB 초과 시 레이어를 세밀하게 분할 적재(Offloading)하기 어려워 `llama.cpp` 엔진으로 전환 결정.

### 2단계: `llama-cpp-python` 및 `ipex-llm` 빌드 트러블슈팅
- **CPU 전용 설치 문제:** 기본 `pip install llama-cpp-python`은 CPU 전용으로 설치되어 GPU 0 사용률이 0%로 나옴.
- **소스 빌드 실패:** `GGML_SYCL=on` 옵션으로 직접 빌드 시 Visual Studio C++ 및 `nmake` 부재로 컴파일 에러 발생.
- **IPEX-LLM 바이너리 시도:** `ipex-llm[cpp]` 사전 빌드 바이너리(`llama-server.exe`) 추출 후 실행했으나, Windows 환경의 `sycl7.dll` / `mkl` 런타임 버전 불일치(`Exit Code -1073741515`)로 종료됨.

### 3단계: 공식 Vulkan 바이너리(`llama.cpp`)로 최종 해결
- **해결책:** `llama.cpp` 공식 릴리즈의 **Windows Vulkan 사전 빌드 바이너리(`bin-win-vulkan-x64.zip`)** 채택.
- **GPU 디바이스 매핑 교정:** Windows 작업 관리자의 `GPU 0`(Intel Arc A350M)이 Vulkan 드라이버에서는 **`Vulkan1`**으로 매핑됨을 확인(`--list-devices`).
- **최종 확정 서버 실행 명령어:**
  ```powershell
  .\llama-server.exe -m "./qwen2.5-coder-3b-instruct-q4_k_m.gguf" --device Vulkan1 --jinja -ngl 14 -c 16384 --temp 0.2 --top-p 0.8 --min-p 0.05 --repeat-penalty 1.15 --host 127.0.0.1 --port 8000
  ```
  - `--device Vulkan1`: 외장 그래픽(Intel Arc A350M) 강제 지정
  - `-ngl 14`: 총 36개 레이어 중 14개를 VRAM에 적재하고 나머지는 시스템 RAM에 분할 오로드하여 OOM 방지
  - `-c 16384`: 에이전트 시스템 프롬프트 수용을 위한 컨텍스트 확장
  - `--temp 0.2 --repeat-penalty 1.15`: 3B 모델의 외국어(중국어) 학습 데이터 환각 및 반복 루프 억제

---

## 3. 에이전트 프레임워크 연동 및 커스텀 루프 개발

### 1단계: DeepSeek Harness (`dsh`) 연동 및 3B 모델의 한계 확인
- 독립형 `pnpm` 설치 후 `deepseek-harness` 소스 빌드 및 `pnpm dsh web` (`http://127.0.0.1:3080`) 구동.
- 로컬 서버(`http://127.0.0.1:8000/v1`) 연결 성공.
- **발견된 한계:** 하네스의 기본 시스템 프롬프트(약 10,000 토큰)가 3B 경량 모델에 너무 무거워 프롬프트 처리 시간이 길고, 정식 `<tool_call>` 태그를 누락한 채 날것의 JSON 텍스트만 출력하거나 엉뚱한 파일(`files.txt`)을 추측하는 현상 발생.

### 2단계: 3B 모델 전용 초경량 Python Agent Loop 직접 구현
- 무거운 하네스 대신 **약 250토큰의 초경량 프롬프트**로 동작하는 자체 대화형 에이전트 루프(`my_agent.py`) 설계.
- **주요 해결 과제:**
  1. **한글 인코딩 충돌 해결:** Windows PowerShell의 `CP949` 출력과 `UTF-8` 충돌(`UnicodeDecodeError`)을 `safe_decode()` 바이너리 디코딩 함수로 해결.
  2. **JSON 파싱 오류 회피:** GUI 코드 작성 시 따옴표/줄바꿈 이스케이프 오류(`Invalid control character`)를 피하기 위해 JSON 대신 직관적인 **`<write file="...">` 및 `<shell>` XML 태그 파싱 방식** 도입.
  3. **문맥 오염 방지 및 자가 수정(Self-Correction):** 매 작업마다 대화 기록을 초기화하여 이전 에러 사과 루프를 차단하고, 실행 에러 발생 시 에러 로그를 모델에 피드백하여 스스로 수정하도록 구현.
  4. **GUI 비동기 실행 처리:** `tkinter` 등 GUI 창 실행 시 타임아웃(`TimeoutExpired`)이 발생하면 에러가 아닌 정상 창 유지 상태로 인식하도록 예외 처리 적용.
