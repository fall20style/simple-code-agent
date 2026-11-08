# 🤖 Intel Arc GPU 기반 완전 로컬 자율 코딩 에이전트 (Local Coding Agent)

외부 클라우드 API나 인터넷 연결 없이, **인텔 아크 외장 그래픽(Intel Arc A350M, VRAM 4GB)** 환경에서 경량 모델(**Qwen2.5-Coder-3B / Qwen3-4B**)을 로컬 서빙하고, 스스로 코드를 작성·실행·수정하는 **자율 코딩 에이전트(Autonomous Coding Agent)** 구축 과제입니다.

기존 단일 에이전트(`my_agent.py`)에서 한 단계 더 나아가, 역할을 분리한 **멀티 에이전트 시스템(`multi_agent_loop-v2.py`)**과 **작업 디렉터리 격리(`--workspace`) / 자동 승인(`--auto`)** 기능을 통합 지원합니다.

---

## 📌 1. 과제 개요 및 목표
- **완전한 오프라인 프라이버시 보장:** 소스코드나 시스템 데이터가 외부로 유출되지 않는 100% 로컬 추론 환경 구축
- **저사양 VRAM(4GB) 하드웨어 한계 극복:** 레이어 분할 적재(Offloading), KV 캐시 양자화(`q4_0`/`q8_0`), Vulkan 백엔드 가속을 통해 경량 모델을 안정적으로 서빙
- **경량 모델 최적화 에이전트 아키텍처 구현:** 무거운 상용 하네스(20,000+ 토큰) 대신 경량 모델의 추론 정확도와 속도를 극대화하는 맞춤형 단일·멀티 에이전트 루프 개발
- **안전한 자율 실행 환경 확보:** `--workspace` 경로 격리 및 위험 명령어 사전 차단 필터를 결합해 안전하게 `--auto` 자동 실행 지원

---

## 🏗️ 2. 시스템 아키텍처

### A. 단일 에이전트 모드 (`my_agent.py`)
```text
[사용자 (CLI 터미널)]
       │  자연어 작업 지시 (예: "구구단 3단 GUI 프로그램 작성 및 실행")
       ▼
[Python Agent Loop (my_agent.py)] ◄──► [Windows PowerShell / 파일 시스템]
       │  OpenAI 호환 REST API (http://127.0.0.1:8000/v1)
       ▼
[llama.cpp Vulkan Server (llama-server.exe)]
       ├──► Intel Arc A350M (GPU 0 / Vulkan1, 전용 VRAM 4GB)
       └──► System RAM (16GB, 잔여 레이어 및 KV 캐시 분산 처리)
```

### B. 멀티 에이전트 모드 (`multi_agent_loop-v2.py`)
```text
[사용자 (CLI 터미널)]
       │  작업 요청 (--auto, --workspace 옵션 지원)
       ▼
[ORCHESTRATOR (총괄 조율자)]
  ├──► [PLANNER (읽기 전용)] : 프로젝트 구조 탐색 및 구현 계획 수립
  ├──► [WORKER (코드 구현)]  : 격리된 워크스페이스 내 파일 생성·수정 및 스크립트 실행
  └──► [REVIEWER (검증)]     : 실행 결과 및 에러 여부 검증 후 최종 보고
```

---

## 💻 3. 개발 및 실행 환경

| 구분 | 사양 및 도구 | 비고 |
| :--- | :--- | :--- |
| **OS** | Windows 11 (x64) | PowerShell 기반 로컬 셸 제어 |
| **GPU** | Intel Arc A350M (전용 VRAM 4GB) | Vulkan 백엔드(`Vulkan1`) 매핑 |
| **RAM / CPU** | 16GB RAM / 12 Threads | 하이브리드 오프로딩 지원 |
| **LLM 모델** | `Qwen2.5-Coder-3B-Instruct` / `Qwen3-4B` (`Q4_K_M`) | 코딩 및 추론 특화 양자화 모델 |
| **추론 엔진** | `llama.cpp` (Windows Vulkan Binary) | OpenAI 호환 API 서버 구동 (`--jinja` 지원) |
| **에이전트 코어** | Python 3.11 + `openai` SDK | 스트리밍 진행률, Tool Calling, 경로 격리 |

---

## ✨ 4. 핵심 구현 기능

### 1) 하드웨어 및 추론 최적화
- **인텔 외장 GPU 타겟팅 및 메모리 최적화:** 내장 그래픽(`GPU 1` 공유 메모리)이 아닌 실제 외장 그래픽(`GPU 0` 전용 VRAM)을 `--device Vulkan1` 옵션으로 강제 지정하고, Flash Attention(`-fa on`) 및 KV 캐시 양자화 적용
- **코딩 특화 샘플링 최적화:** `--presence-penalty`를 제거(`0.0`)하고 낮은 `--temp 0.2`를 적용하여 변수명(`self.`, `def` 등) 반복 억제로 인한 문법 오류와 불필요한 토큰 낭비 방지
- **`/no_think` 사고 토큰 차단:** Qwen3 모델 사용 시 `enable_thinking: False`와 `/no_think` 지시어를 결합해 불필요한 긴 추론 과정 없이 즉시 도구 호출 및 코드 출력

### 2) 에이전트 코어 및 멀티 에이전트 아키텍처
- **역할 기반 권한 분리 (Orchestrator / Planner / Worker / Reviewer):** 읽기 전용 탐색(Planner)과 코드 수정(Worker), 검증(Reviewer)의 허용 도구를 엄격히 분리해 환각 및 오작동 최소화
- **실시간 스트리밍 진행률 표시:** 긴 코드를 생성하는 동안 터미널에 생성 글자 수와 경과 시간을 실시간 표시해 멈춤 오인 방지
- **자가 수정(Self-Correction) 루프:** 명령어 실행 실패(`returncode != 0` 또는 `stderr` 발생) 시 에러 로그를 모델에게 자동 피드백하여 스스로 코드를 수정하고 재실행

### 3) 보안 및 Windows 환경 특화 기능
- **작업 디렉터리 격리 (`--workspace`):** 지정된 작업 폴더 외부로의 파일 읽기·쓰기·수정 및 `..` 경로 탈출(Path Traversal)을 원천 차단
- **자동 승인 모드 (`--auto`) 및 다중 안전장치:** 매 단계 `y/n` 수동 승인 없이 자율 실행하되, 위험 시스템 명령어(`Remove-Item -Recurse -Force`, `format`, `shutdown` 등)는 `--auto` 모드에서도 무조건 차단
- **Windows 예외 처리:** `CP949`·`UTF-8` 안전 디코딩(`safe_decode`) 및 `tkinter`/`pygame` GUI 앱 실행 시 2.5초 초기 오류 감시 후 비동기 백그라운드 전환 처리

---

## 🚀 5. 설치 및 실행 가이드

### 1단계: 사전 준비
1. [llama.cpp 공식 릴리즈](https://github.com/ggml-org/llama.cpp/releases)에서 Windows Vulkan 바이너리(`bin-win-vulkan-x64.zip`)를 다운로드하여 작업 폴더에 압축을 풉니다.
2. 양자화 모델 파일(`Qwen3-4B-Q4_K_M.gguf` 또는 `qwen2.5-coder-3b-instruct-q4_k_m.gguf`)을 배치합니다.
3. 파이썬 필수 패키지를 설치합니다.
   ```powershell
   pip install openai
   ```

### 2단계: 로컬 LLM API 서버 구동 (터미널 1)

* **Qwen3-4B 권장 실행 (멀티 에이전트 / Tool Calling 최적화):**
  ```powershell
  .\llama-server.exe -m "./Qwen3-4B-Q4_K_M.gguf" --device Vulkan1 --jinja -ngl 99 -np 1 -c 16384 -fa on -b 2048 -ub 512 --cache-type-k q8_0 --cache-type-v q8_0 --temp 0.2 --top-p 0.9 --top-k 20 --min-p 0.0 --host 127.0.0.1 --port 8000
  ```
* **Qwen2.5-Coder-3B 실행 (4GB VRAM 레이어 분할 적재):**
  ```powershell
  .\llama-server.exe -m "./qwen2.5-coder-3b-instruct-q4_k_m.gguf" --device Vulkan1 --jinja -ngl 14 -c 16384 --temp 0.2 --top-p 0.8 --min-p 0.05 --repeat-penalty 1.15 --host 127.0.0.1 --port 8000
  ```

### 3단계: 자율 코딩 에이전트 실행 (터미널 2)

* **멀티 에이전트 모드 실행 (격리 폴더 + 자동 승인 권장):**
  ```powershell
  python .\multi_agent_loop-v2.py --auto --workspace .\sandbox
  ```
* **단일 에이전트 모드 실행:**
  ```powershell
  python .\my_agent.py
  ```

#### CLI 주요 옵션 (`multi_agent_loop-v2.py`)
| 옵션 | 설명 | 기본값 |
| :--- | :--- | :--- |
| `--workspace <경로>` | 에이전트가 접근·수정할 수 있는 작업 폴더를 격리 제한 | 현재 실행 폴더 (`.`) |
| `--auto` | 파일 저장/수정 및 PowerShell 실행 시 `y/n` 승인 없이 자동 실행 | 비활성화 (수동 승인) |

---

## 📂 6. 프로젝트 파일 구성
- `multi_agent_loop-v2.py`: Planner / Worker / Reviewer 역할 분리, `--workspace` 경로 격리, `--auto` 자동 승인 모드가 통합된 최신 멀티 에이전트 스크립트
- `my_agent.py`: 대화형 입력, XML/마크다운 하이브리드 파싱, 자가 수정 루프가 구현된 경량 단일 에이전트 스크립트
- `multi_agent_optimization_guide-v2.md`: `llama-server` 샘플링 최적화(`--presence-penalty` 제거), 성능 벤치마크, 트러블슈팅 통합 기술 문서
- `multi_agent_loop_v2_explanation.md`: `--workspace` 경로 탈출 차단 및 `--auto` 안전 필터 구조 해설 문서
- `README.md`: 과제 개요, 시스템 아키텍처, 설치 및 실행 방법을 정리한 프로젝트 소개 문서
