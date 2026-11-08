# 🏗️ HARNESS-ARCHITECTURE.md: 로컬 LLM 자율 코딩 에이전트 하네스 설계서

> **대상 모델 및 환경:** `Qwen3-4B-Q4_K_M` / `Qwen2.5-Coder-3B` + `llama.cpp (Vulkan1)` + Windows 11  
> **관련 구현체:** `singe_agent_loop.py` (단일 에이전트), `multi_agent_loop-v3.py` (멀티 에이전트)

---

## 1. 에이전트 하네스(Agent Harness)의 정의와 설계 철학

**에이전트 하네스(Harness)**란 순수 언어 모델(LLM)이 텍스트 예측을 넘어 실제 로컬 PC 환경에서 **파일을 탐색·수정하고, 명령을 실행하며, 오류를 자가 복구하도록 감싸는 모든 제어 시스템과 안전장치의 총합**을 의미합니다.

### 왜 경량 전용 하네스를 직접 설계했는가?
* **상용 CLI 하네스(Qwen Code 등)의 한계:** 초기 시스템 프롬프트와 방대한 도구 정의만으로 **21,000+ 토큰**을 점유하여, 4GB VRAM 및 3B~4B급 로컬 모델 환경에서 프롬프트 연산(`Prefill`)에만 4분 이상 소요되고 클라이언트 타임아웃을 유발함.
* **초경량 맞춤형 하네스 전략:** 초기 컨텍스트를 **약 1,500~2,000 토큰 수준(1/10 이하)**으로 압축하고, 소형 모델 특유의 JSON 파싱 오류와 Windows PowerShell 환경 제약을 코드 레벨에서 보정하여 실사용 가능한 속도와 안정성을 확보함.

---

## 2. 전체 하네스 아키텍처 구조도

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        사용자 (CLI / 자동 승인 모드)                       │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                   [1] 실행 루프 & 상태 제어 (Control Loop)                │
│  • ReAct 반복 루프 (MAX_STEPS)   • 컨텍스트 슬라이딩 윈도우 (trim_context)  │
│  • 실시간 스트리밍 & 반복 출력(//////) 조기 차단 가드레일                    │
└──────────────┬───────────────────────────────────────────┬──────────────┘
               ▼                                           ▼
┌──────────────────────────────┐            ┌─────────────────────────────┐
│ [2] 파싱 & 자가 복구 계층     │            │ [3] 보안 & 권한 통제 계층    │
│ • <think> 추론 태그 제거      │            │ • --workspace 경로 격리     │
│ • 표준 Tool Call + Fallback  │            │ • 위험 명령어 정규식 필터    │
│ • 에러 피드백 자가 수정 루프   │            │ • 승인 게이트 (--auto 전환) │
└──────────────┬───────────────┘            └──────────────┬──────────────┘
               └─────────────────────┬─────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│             [4] Windows 특화 런타임 어댑터 (Tool Runtime)                │
│  • 파일 시스템 (list_dir, glob, grep, read_file, write_file, edit_file) │
│  • PowerShell 어댑터 (UTF-8 강제 디코딩 + GUI 앱 Popen 비동기 실행 감지)  │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│             [5] 로컬 추론 브리지 (Local Serving Bridge)                  │
│  • 프록시 우회 (no_proxy)  • /no_think 강제  • 역할을 고려한 max_tokens   │
│  • llama-server (Vulkan1, --jinja, -fa off, --repeat-penalty 1.1)       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 현재 구현된 핵심 하네스 요소 상세

### 3.1. 실행 루프 및 상태 제어 (Execution Loop & State Control)
* **ReAct(Reason + Act) 다단계 루프:** 모델이 한 번의 응답으로 종료하지 않고 `[계획 → 도구 실행 → 결과 관찰 → 수정/완료]` 사이클을 `MAX_STEPS` 한도 내에서 자율 반복.
* **스트리밍 기반 반복 폭주(Degeneration) 조기 차단:**
  * 소형 모델이 특정 토큰(`//////` 등)을 무한 반복 출력할 때 `max_tokens`까지 대기하지 않고, 최근 출력 청크의 문자 다양성을 실시간 검사해 **0.5초 이내에 스트림을 강제 중단**하는 가드레일 내장.
* **슬라이딩 윈도우 컨텍스트 관리 (`trim_context`):**
  * 대화 이력이 `MAX_HISTORY`를 초과하면 최상단 시스템 프롬프트(`messages[0]`)는 보존한 채 오래된 중간 턴만 제거하여 컨텍스트 초과(OOM) 방지.

### 3.2. 파싱 및 오류 복구 계층 (Parser & Self-Correction)
* **사고 토큰 제거 (`<think>` Sanitizer):** Qwen3 계열 모델이 출력할 수 있는 `<think>...</think>` 블록을 정규식으로 제거해 후속 파서 오염 차단.
* **이중 Tool Calling 파서 (Standard + Regex Fallback):**
  * 1차로 `llama-server --jinja`가 반환하는 표준 `msg.tool_calls`를 처리.
  * 2차로 소형 모델이 일반 텍스트나 마크다운 코드블록(` ```json `)으로 도구를 호출하거나, 삼중 따옴표(`"""`) 등 비표준 JSON을 내뱉을 경우 `_repair_pseudo_json`과 `ast.literal_eval`로 자동 복구.
* **에러 피드백 자가 수정 루프:** JSON 파싱 실패나 PowerShell 실행 오류(`STDERR`, `ExitCode != 0`) 발생 시 예외를 삼키고 에러 원문을 `user`/`tool` 메시지로 재주입하여 모델이 스스로 코드를 고치도록 유도.

### 3.3. 보안 및 권한 통제 가드레일 (Security & Guardrails)
* **작업 디렉터리 격리 (`--workspace` / `resolve_workspace_path`):** 모든 파일 읽기·쓰기·탐색 경로를 지정된 작업 폴더 내부(`os.path.commonpath` 검증)로 제한하여 `../` 경로 탈출이나 시스템 파일 훼손 원천 차단.
* **위험 명령어 사전 차단 (`check_dangerous_command`):** `Remove-Item -Recurse -Force`, `format`, `diskpart`, `shutdown` 등 파괴적 명령 패턴을 실행 전 정규식으로 탐지하여 즉시 차단.
* **실행 승인 정책 (`--auto` vs `Interactive Gate`):** 기본값은 파일 쓰기·수정·명령 실행 전 사용자 승인(`y/n`)을 요구하며, 검증된 격리 환경에서는 `--auto` 플래그로 무인 자동화 전환 가능.
* **역할 기반 최소 권한 분리 (`multi_agent_loop`):** `Planner`는 읽기 전용 도구(`list_dir`, `glob`, `grep`, `read_file`)만 허용하고, `Worker`에게만 쓰기/수정 권한을 부여.

### 3.4. Windows 특화 런타임 어댑터 (Tool Runtime)
* **인코딩 충돌 방어 (`safe_decode`):** Windows PowerShell의 기본 `CP949` 출력과 Python `UTF-8` 환경 간의 디코딩 깨짐을 방지하기 위해 콘솔 인코딩을 UTF-8로 강제하고 다중 인코딩 폴백 처리.
* **GUI 애플리케이션 비동기 실행 감지:** `tkinter`, `pygame` 등 창이 열려 유지되는 GUI 스크립트 실행 시 `subprocess.run`이 무한 대기(타임아웃)에 빠지지 않도록 `Popen`으로 2.5초간 초기 문법·런타임 에러만 감시한 뒤 정상 실행 완료로 처리.

### 3.5. 컨텍스트 주입 및 서빙 브리지 (Context & Serving Bridge)
* **프로젝트 지침 자동 로더 (`load_project_docs`):** 작업 폴더 내 `AGENT.md`, `CLAUDE.md`, `README.md` 등을 지정된 토큰 예산(`MAX_PROJECT_DOC_CHARS`) 내에서 자동 로드해 일관된 코딩 컨벤션 유지.
* **로컬 추론 최적화 파라미터 연동:** `no_proxy` 설정, `enable_thinking: False` + `/no_think` 주입, 역할별 `max_tokens` 차등 제한 적용.

---

## 4. 향후 추가해야 할 하네스 요소 (Roadmap / TODO)

현재의 경량 하네스를 프로덕션 수준의 신뢰성과 장기 작업(Long-Horizon Task) 처리 능력으로 끌어올리기 위해 향후 추가할 핵심 고도화 항목입니다.

### 🔴 우선순위 높음 (안정성 및 복구력 강화)
1. **파일 수정 전 자동 스냅샷 & 롤백(Undo / Checkpoint) 시스템**
   * **필요성:** `--auto` 모드에서 `Worker`가 기존 동작하던 코드를 잘못 수정해 망가뜨릴 경우, 이전 정상 상태로 즉시 되돌릴 수 있는 안전망이 필요함.
   * **구현 방향:** `write_file` / `edit_file` 실행 직전 대상 파일을 `.agent_backup/<timestamp>_<filename>`에 자동 백업하고, `/undo` 명령어나 테스트 실패 시 자동 복원하는 도구 추가.
2. **구문 검사(AST / Linter) 사전 검증 게이트**
   * **필요성:** 파이썬 파일을 저장한 뒤 실행해서 에러를 발견하는 왕복 시간(1턴 = 수십 초)을 줄여야 함.
   * **구현 방향:** `write_file` 및 `edit_file` 내부에서 저장 직후 Python 내장 `ast.parse()` 또는 `py_compile`을 즉시 수행하여, 문법 오류(SyntaxError, IndentationError)가 있으면 LLM 호출 없이 도구 반환값으로 즉시 에러 줄 번호를 알려줌.
3. **무한 도구 호출 루프(Semantic Loop) 감지기**
   * **필요성:** 소형 모델이 동일한 파일에 동일한 `edit_file`이나 실패한 `run_powershell` 명령을 3회 이상 똑같이 반복 호출하는 현상 방지.
   * **구현 방향:** 최근 호출된 `(tool_name, args_hash)` 이력을 추적하여 동일한 호출이 2회 이상 반복되면 강제로 경고를 주입하거나 다른 접근법을 쓰도록 유도.

### 🟡 우선순위 중간 (컨텍스트 효율 및 작업 능력 확장)
4. **지능형 컨텍스트 압축(Context Compaction / Summarization)**
   * **필요성:** 현재 `trim_context`는 오래된 메시지를 단순 삭제하므로, 긴 작업 중 초반에 파악한 파일 구조나 핵심 요구사항이 유실될 수 있음.
   * **구현 방향:** 토큰 사용량이 임계치(예: 10,000 토큰)에 도달하면, 완료된 탐색·수정 이력을 핵심 사실 위주의 짧은 요약본으로 압축(Compaction)하여 `messages[1]`에 고정 유지.
5. **줄 번호 기반 정밀 파일 읽기 (`read_file` 부분 조회)**
   * **필요성:** 수백 줄이 넘는 큰 파일을 통째로 `read_file`로 읽으면 컨텍스트 창이 순식간에 차고 `Prompt Eval` 속도가 급감함.
   * **구현 방향:** `read_file(path, start_line, end_line)` 파라미터를 추가하고, 출력 시 각 줄 앞에 줄 번호(`1: import os`)를 붙여 `edit_file` 정확도와 토큰 효율을 동시에 개선.
6. **실행 트레이스 로깅 및 관측성(Observability / Telemetry)**
   * **필요성:** 에이전트가 어떤 단계에서 몇 토큰을 썼고, 어느 도구에서 실패했는지 사후 분석하여 프롬프트와 하네스를 개선하기 위함.
   * **구현 방향:** 매 세션의 스텝별 소요 시간, 토큰 수, 도구 호출 인자, 성공/실패 여부를 `logs/session_<timestamp>.jsonl` 형태로 자동 기록.

### 🟢 우선순위 낮음 (확장성 및 사용자 경험)
7. **외부 도구 표준 프로토콜(MCP: Model Context Protocol) 경량 클라이언트**
   * **필요성:** 웹 검색, DB 조회, 브라우저 제어 등 외부 도구를 코드 수정 없이 플러그인 형태로 연결.
   * **구현 방향:** 로컬에서 필요한 최소한의 MCP 서버만 선택적으로 바인딩(Lazy Loading)하여 초기 프롬프트 토큰 증가를 최소화하면서 도구 확장.
8. **중단된 작업 재개(Session Persistence / `--resume`)**
   * **필요성:** 터미널 종료나 예기치 않은 중단 후에도 이전 대화 상태와 작업 이력을 그대로 이어서 진행.
   * **구현 방향:** 현재 `messages` 배열과 `workspace` 상태를 JSON 파일로 직렬화하여 저장하고 `--resume` 플래그로 복원.



# 📋 에이전트 하네스(Agent Harness) 전체 기능 목록 (DONE / TODO)

> **진행 현황 요약:** 전체 30개 핵심 기능 중 **DONE 20개 (66.7%)** / **TODO 10개 (33.3%)**

---

## 1. 실행 루프 및 상태 제어 (Execution Loop & State Control)
* **1. [DONE]** ReAct / Tool-Use 반복 실행 루프 제어 (`Thought → Action → Observation`)
* **2. [DONE]** 최대 실행 단계(`MAX_STEPS`) 제한 및 강제 종료 처리
* **3. [DONE]** 실시간 스트리밍 출력 및 토큰 생성 경과 시간 표시 (`stream_chat_completion`)
* **4. [DONE]** 동일 문자/토큰 연속 반복 출력(Degeneration) 조기 감지 및 중단
* **5. [TODO]** 동일 도구·동일 인자 무한 반복 호출(Stuck Loop) 감지 및 차단
* **6. [TODO]** 대화 히스토리 및 작업 상태 저장/복원 (`--continue`, `--resume` 세션 체크포인트)

---

## 2. 도구(Tools) 인터페이스 및 코드 조작 (Tooling & Code Manipulation)
* **7. [DONE]** 디렉터리 탐색 및 파일 검색 도구 (`list_dir`, `glob`, `grep`)
* **8. [DONE]** 파일 전체 읽기/쓰기/부분 치환 도구 (`read_file`, `write_file`, `edit_file`)
* **9. [TODO]** 대용량 파일 부분 읽기 지원 (`start_line`, `end_line` 기반 라인 범위 조회 및 줄 번호 표시)
* **10. [TODO]** 파일 수정 전 자동 스냅샷 백업 및 작업 실패 시 롤백(`/undo`) 기능
* **11. [TODO]** 코드 저장 직후 정적 구문/린트 사전 검증 (`ast.parse`, `ruff` 등 실행 전 문법 오류 즉시 피드백)
* **12. [DONE]** 멀티 에이전트 역할 분리 및 작업 위임 (`Planner` / `Worker` / `Reviewer` 오케스트레이션)

---

## 3. 파싱 및 자가 수정 복구 (Parsing & Self-Correction)
* **13. [DONE]** 표준 OpenAI Tool Calling(`tool_calls`) 및 텍스트 기반 폴백(Fallback) JSON 이중 파싱
* **14. [DONE]** 추론 태그(`<think>...</think>`) 및 마크다운 코드 블록 자동 정제
* **15. [DONE]** 비표준 JSON(삼중 따옴표, 백틱, 이스케이프 누락 등) 자동 복구 파서 (`_repair_pseudo_json`)
* **16. [DONE]** 도구 실행 실패 및 셸 에러(`STDERR`) 자동 피드백 기반 자가 수정 루프

---

## 4. 보안 및 권한 통제 가드레일 (Security & Guardrails)
* **17. [DONE]** 작업 디렉터리 격리(`--workspace`) 및 상위 경로 탈출(`../`, 심볼릭 링크) 차단
* **18. [DONE]** 위험 시스템 명령어 정규식 필터링(`Remove-Item -Recurse -Force`, `format`, `shutdown` 등 차단)
* **19. [TODO]** 민감 파일(`.env`, SSH 키, 인증서 등) 읽기/쓰기 접근 차단
* **20. [DONE]** 실행 보안 모드 전환 (`INTERACTIVE` 수동 승인 게이트 vs `--auto` 자동 승인 모드)
* **21. [TODO]** 셸 명령어 화이트리스트/블랙리스트 기반 세분화 권한 정책

---

## 5. 컨텍스트 및 메모리 관리 (Context & Memory Management)
* **22. [DONE]** 프로젝트 규칙 문서(`AGENT.md`, `CLAUDE.md`, `README.md`) 자동 로드 및 시스템 프롬프트 주입
* **23. [DONE]** 슬라이딩 윈도우 기반 대화 기록 트리밍(`trim_context`)
* **24. [TODO]** 장기 대화 시 오래된 도구 실행 결과를 핵심 요약으로 압축하는 컨텍스트 컴팩션(Compaction)
* **25. [TODO]** 긴 도구 출력(`STDOUT`/`STDERR`)의 앞·뒤 핵심 보존 및 중간 절삭(Truncation)

---

## 6. OS 런타임 및 로컬 LLM 브리지 (Runtime & Serving Bridge)
* **26. [DONE]** Windows PowerShell 입출력 인코딩(`UTF-8` / `CP949`) 자동 보정
* **27. [DONE]** GUI 및 백그라운드 프로세스 비동기 실행 감지(무한 대기 타임아웃 방지)
* **28. [DONE]** 서브프로세스 실행 시간 상한 및 강제 종료(Timeout & Kill) 처리
* **29. [DONE]** 로컬 추론 서버(`llama-server`) 프록시 우회 및 사고 모드 제어(`/no_think`)
* **30. [TODO]** 토큰 사용량, 단계별 소요 시간, 성공/실패 이력 구조화 로깅(Audit Log / Telemetry)

