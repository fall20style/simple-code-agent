# 로컬 LLM 기반 Python 멀티 에이전트 코딩 시스템 구축 및 최적화 기술 문서

## 1. 시스템 아키텍처 개요
* **로컬 추론 엔진:** `llama-server.exe` (Vulkan1 백엔드, `Qwen3-4B-Q4_K_M.gguf`)
* **에이전트 구조:** 오케스트레이터(Orchestrator)가 작업을 분배하고, 권한이 분리된 3개의 서브 에이전트(`Planner`, `Worker`, `Reviewer`)가 표준 Tool Calling을 통해 순차 실행하는 구조
* **통신 프로토콜:** OpenAI 호환 로컬 API (`http://127.0.0.1:8000/v1`)

---

## 2. `llama-server` 추론 파라미터 최적화

### 2.1 `--presence-penalty 1.5` 제거의 의미
`--presence-penalty`는 한 번 등장한 토큰을 다시 생성하지 않도록 벌점(Penalty)을 부과하는 샘플링 파라미터입니다.

1. **프로그래밍 문법 및 필수 키워드 반복 보호:**
   * 파이썬 코드에서는 `self.`, `def `, `return`, `import`, `if`, `for`, `tkinter.` 등의 키워드와 동일한 변수명이 수십 번 반복되어야 합니다.
   * `1.5`의 강한 페널티가 적용되면 모델이 기존 변수를 재사용하지 않고 억지로 동의어 변수를 새로 생성하거나 우회 로직을 작성하여 코드가 길어지고 문법 오류가 급증합니다.
2. **JSON 및 Tool Calling 스키마 안정성 확보:**
   * `"name"`, `"arguments"`, `"path"`, `"content"` 등 반복되는 표준 JSON 키 구조를 회피하려다 파싱 오류가 발생하는 문제를 예방합니다.
3. **총 출력 토큰 수(`n_gen`) 감소:**
   * 불필요한 우회 코드가 사라져 전체 출력 토큰 수가 줄어들고, 체감 응답 대기 시간이 단축됩니다.

### 2.2 컨텍스트(`-c`) 및 샘플링 파라미터 튜닝
* **컨텍스트 크기 축소 (`32768` → `16384` 또는 `8192`):** 21k 이상의 대형 CLI 프롬프트 대신 경량화된 자체 에이전트 프롬프트를 사용하므로, 불필요한 KV 캐시 VRAM 예약을 줄여 시스템 RAM 스필오버(병목)를 방지합니다.
* **결정론적 샘플링 (`--temp 0.2`):** 코딩 및 JSON 생성 정확도를 높이기 위해 온도를 낮춥니다.

### 2.3 권장 `llama-server` 실행 명령어
```powershell
.\llama-server.exe -m "./Qwen3-4B-Q4_K_M.gguf" --device Vulkan1 --jinja -ngl 99 -np 1 -c 16384 -fa on -b 2048 -ub 512 --cache-type-k q8_0 --cache-type-v q8_0 --temp 0.2 --top-p 0.9 --top-k 20 --min-p 0.0 --host 127.0.0.1 --port 8000
```

| 파라미터 | 변경 전 | 변경 후 | 기대 효과 |
| :--- | :---: | :---: | :--- |
| `--presence-penalty` | `1.5` | **제거 (`0.0`)** | 변수·키워드 일관성 유지, 불필요한 코드 길이 및 문법 에러 감소 |
| `--temp` | `0.6` | **`0.2`** | JSON Tool Calling 및 파이썬 문법 정확도 향상 |
| `-c` | `32768` / `24576` | **`16384`** | 전용 VRAM 점유율 최적화로 프롬프트 처리 및 생성 속도 안정화 |
| `-fa` | 미지정 | **`on`** | 긴 컨텍스트 어텐션 연산 가속 및 VRAM 절감 |

---

## 3. `multi_agent_loop.py` 트러블슈팅 내역

### 문제 1: 긴 코드 생성 중 터미널 무응답(멈춤 현상)
* **원인:** `WORKER` 에이전트가 전체 게임 소스 코드(1,500~2,500 토큰)를 생성하는 동안 비스트리밍(`stream=False`) 방식으로 대기하여 화면에 진행 상태가 표시되지 않음.
* **해결:** `stream=True` 기반 `stream_chat_completion()` 함수를 구현해 실시간 생성 글자 수와 경과 시간(`⏳ [worker 생성 중...] 1,240자 (18.5초)`)을 터미널에 실시간 출력.

### 문제 2: Qwen3 추론 토큰(`<think>`)으로 인한 지연 및 파싱 오류
* **원인:** Qwen3 모델이 기본값으로 긴 `<think>...</think>` 사고 블록을 출력하여 불필요한 토큰 생성 시간이 소요되고 JSON 파싱을 방해함.
* **해결:**
  1. 모든 시스템 프롬프트 상단에 `/no_think` 지시어 추가
  2. API 호출 시 `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` 전달
  3. 후처리 정규식(`re.sub(r'<think>.*?</think>', ...)`)으로 잔여 태그 완전 제거

### 문제 3: Windows GUI 프로그램(`tkinter` / `pygame`) 실행 시 30초 타임아웃
* **원인:** `subprocess.run(..., timeout=30)`으로 GUI 창을 실행하면 사용자가 창을 닫을 때까지 프로세스가 종료되지 않아 30초 후 타임아웃 에러 발생.
* **해결:** `python *.py` 실행 명령을 감지해 `subprocess.Popen`으로 비동기 실행하고, `communicate(timeout=2.5)`로 초기 2.5초간 문법·런타임 에러만 감시한 뒤 정상 유지 시 즉시 완료 처리.

### 문제 4: 로컬 환경 프록시 간섭 및 모델명 불일치
* **원인:** 시스템 프록시 설정이 `localhost:8000` 요청을 가로채거나, 기본 `MODEL_NAME`이 실제 로드된 GGUF 모델명과 다름.
* **해결:** 스크립트 시작 시 `http_proxy`, `https_proxy`를 초기화하고 `no_proxy="localhost,127.0.0.1"` 및 `MODEL_NAME = "Qwen3-4B-Q4_K_M.gguf"`로 고정.

---

## 4. 성능 벤치마크 비교표 (수정 전 vs 수정 후)

동일 하드웨어(`Vulkan1`, `Qwen3-4B-Q4_K_M.gguf`) 환경에서 **"Python으로 테트리스 게임을 작성하여 실행"** 작업을 수행했을 때의 실측 기반 비교 지표입니다.

| 측정 항목 | 수정 전 (초기 CLI / 기본 에이전트) | 수정 후 (최적화 `multi_agent_loop.py`) | 개선 효과 |
| :--- | :--- | :--- | :--- |
| **초기 프롬프트 크기** | `21,256` 토큰 (CLI 기본 도구 포함) | `~1,778` 토큰 (경량 멀티 에이전트) | **프롬프트 크기 약 91% 감소** |
| **프롬프트 처리 시간 (Prefill)** | `223초+` (4분 초과 타임아웃 발생) | **`8.9초`** (`198.91 t/s`) | **약 25배 단축 (타임아웃 완전 해소)** |
| **불필요한 `<think>` 토큰** | 매 스텝 `500 ~ 1,000+` 토큰 낭비 | **`0` 토큰** (`/no_think` 적용) | **응답당 대기 시간 40~60초 절감** |
| **평균 토큰 생성 속도 (`tg`)** | `7.05 ~ 8.05 t/s` (RAM 스필오버) | **`8.39 ~ 9.88 t/s`** (안정 유지) | **생성 속도 및 일관성 향상** |
| **GUI 실행 단계 소요 시간** | `30초` 대기 후 `TimeoutExpired` 실패 | **`2.5초`** 내 초기 검증 후 즉시 통과 | **실행 검증 시간 12배 단축** |
| **전체 작업 완료까지 왕복 수** | `7회` 도달 후 실패 (파싱/셸 에러 반복) | **`3~4회`** (Planner → Worker → Reviewer) | **1회 시도 만에 정상 실행 완료** |

---

## 5. 추가 확장 기능 설계 가이드

### 5.1 자동 승인 모드 (`--auto` CLI 플래그) 설계
현재 `write_file`, `edit_file`, `run_powershell` 실행 시마다 사용자에게 `y/n` 입력을 요구합니다. 신뢰할 수 있는 로컬 작업 폴더에서 완전 자율 실행을 지원하려면 아래 구조로 확장할 수 있습니다.

1. **CLI 인자 파싱 (`argparse`):**
   ```python
   import argparse

   parser = argparse.ArgumentParser(description="Multi-Agent Coding Loop")
   parser.add_argument("--auto", action="store_true", help="위험 명령어를 제외한 파일 쓰기 및 실행 자동 승인")
   ARGS = parser.parse_args()
   ```
2. **안전 필터 연동 승인 분기 (`execute_base_tool` 내부):**
   * `check_dangerous_command(cmd)`에 걸리는 파괴적 명령어(`Remove-Item -Recurse -Force`, `format` 등)는 `--auto` 모드에서도 **무조건 차단**을 유지합니다.
   * 일반 `write_file`, `edit_file`, `run_powershell`만 `ARGS.auto`가 `True`일 때 `input()` 프롬프트를 건너뛰고 자동 승인합니다.
   ```python
   if not ARGS.auto:
       confirm = input(f"\n⚠️ [{caller_tag} 승인] '{args['path']}' 파일을 저장할까요? (y/n): ")
       if confirm.lower() != "y":
           return "User rejected file write."
   else:
       print(f"  ⚡ [{caller_tag} 자동 승인] '{args['path']}' 저장 실행")
   ```

### 5.2 작업 디렉터리 격리(Sandbox Workspace) 확장
* 에이전트가 프로젝트 루트 파일을 실수로 덮어쓰지 않도록 전용 작업 폴더(예: `./workspace`)를 생성하고, 모든 파일 경로(`path`)가 해당 디렉터리 내부인지 `os.path.abspath()`로 검증하는 가드레일을 추가하면 `--auto` 모드에서도 안전하게 운용할 수 있습니다.

### 5.3 컨텍스트 압축(Context Summarization) 확장
* 대화가 길어져 `MAX_HISTORY`를 초과할 때 단순히 오래된 메시지를 잘라내는(`trim_context`) 대신, 완료된 서브 에이전트의 작업 결과 요약을 `system` 프롬프트 하단의 `[Completed Milestones]` 섹션에 누적 기록하면 장기 프로젝트에서도 맥락 유실 없이 작업을 이어갈 수 있습니다.
