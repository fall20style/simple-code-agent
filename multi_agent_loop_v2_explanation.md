# `multi_agent_loop-v2.py` 코드 상세 해설: 자동 승인 모드(`--auto`) & 작업 디렉터리 격리(`--workspace`)

## 1. 개요
`multi_agent_loop-v2.py`는 로컬 LLM(`Qwen3-4B-Q4_K_M.gguf`) 기반의 멀티 에이전트 코딩 시스템(Orchestrator / Planner / Worker / Reviewer)을 **무인 자동화 환경에서 안전하게 실행**할 수 있도록 고도화한 버전입니다.

기존 v1 버전은 파일 저장(`write_file`), 수정(`edit_file`), PowerShell 실행(`run_powershell`) 시마다 사용자에게 `y/n` 입력을 요구해 자동화 흐름이 끊기는 불편함이 있었으며, 에이전트가 잘못된 경로를 지정해 프로젝트 루트나 시스템 중요 파일을 덮어쓸 위험이 존재했습니다. 

v2에서는 이를 해결하기 위해 **① 작업 디렉터리 격리(`--workspace`)**를 선행 구축하여 물리적·논리적 안전 경계(Sandbox)를 만든 뒤, 그 위에서 **② 자동 승인 모드(`--auto`)**를 안심하고 가동할 수 있도록 설계했습니다.

---

## 2. 핵심 기능 상세 해설

### ① 작업 디렉터리 격리 (`--workspace`)

에이전트의 모든 파일 탐색, 읽기/쓰기, 명령 실행 범위를 사용자가 지정한 특정 폴더(`workspace`) 내부로 엄격히 제한하는 샌드박스(Sandbox) 메커니즘입니다.

#### 1) 경로 검증 및 탈출 차단 (`resolve_workspace_path`)
* **방어 대상:** 상대 경로 상위 이동(`../../`), 절대 경로 직접 지정(`C:\Windows\...`), 심볼릭 링크를 통한 작업 폴더 외부 접근(Path Traversal 공격 및 LLM 환각에 의한 오작동)
* **동작 원리:**
  1. `os.path.abspath(workspace_dir)`로 기준 작업 폴더의 절대 경로를 확정합니다.
  2. 에이전트가 요청한 대상 경로를 `os.path.join(workspace_dir, target_path)`로 결합한 뒤 `os.path.abspath()`로 정규화합니다.
  3. `os.path.commonpath([workspace_abs, target_abs]) == workspace_abs` 조건을 검사하여, 정규화된 최종 경로가 기준 폴더 하위 트리에 속하는지 수학적으로 검증합니다.
  4. 기준 폴더를 벗어나는 경우 즉시 `PermissionError` 예외를 발생시켜 도구 실행을 차단하고 에이전트에게 오류 메시지를 반환합니다.

#### 2) 셸 실행 위치 고정 (`cwd=workspace_dir`)
* `run_powershell` 및 Python 스크립트(`subprocess.Popen` / `subprocess.run`) 실행 시 `cwd=workspace_dir` 인자를 강제 지정합니다.
* 에이전트가 `python tetris.py`나 `ls` 같은 명령을 내릴 때 항상 격리된 작업 폴더 내부를 기준점으로 동작하므로, 메인 프로젝트 코드가 오염되지 않습니다.

#### 3) 적용 범위 (전 도구 일괄 통제)
* **읽기/탐색 도구:** `list_dir`, `glob`, `grep`, `read_file` 모두 격리된 디렉터리 내부만 조회합니다.
* **쓰기/실행 도구:** `write_file`, `edit_file`, `run_powershell` 모두 검증된 경로 내에서만 수행됩니다.

---

### ② 자동 승인 모드 (`--auto`)

사람의 개입(`y/n` 프롬프트 대기) 없이 에이전트가 계획 수립부터 코드 작성, 실행 검증까지 한 번에 완주하도록 지원하는 기능입니다.

#### 1) CLI 플래그 제어 (`argparse`)
* 스크립트 실행 시 `--auto` 플래그 전달 여부에 따라 `auto_approve: bool` 상태가 결정됩니다.
* `--auto` 미지정 시(기본값 `False`): 기존과 동일하게 쓰기/실행 전 사용자에게 `y/n` 승인을 요청합니다.
* `--auto` 지정 시(`True`): 승인 대기 프롬프트를 건너뛰고 즉시 실행하며, 터미널에는 `[AUTO] 자동 승인됨` 로그만 간결하게 출력합니다.

#### 2) 자동 승인 모드에서도 유지되는 다중 안전장치
자동 승인 모드를 켜더라도 시스템 파괴를 막기 위한 최소한의 하드코딩 방어선은 해제되지 않고 계속 작동합니다.

| 안전장치 계층 | 방어 내용 | `--auto` 모드 시 동작 |
| :--- | :--- | :--- |
| **1. 경로 격리 (`resolve_workspace_path`)** | `../` 등을 이용한 워크스페이스 외부 파일 접근/수정 차단 | **항상 활성 (차단)** |
| **2. 위험 명령어 블랙리스트 (`DANGEROUS_PATTERNS`)** | `Remove-Item -Recurse -Force`, `format`, `diskpart`, `shutdown` 등 시스템 파괴 명령 차단 | **항상 활성 (차단)** |
| **3. 역할별 도구 권한 분리 (`ROLE_ALLOWED_TOOLS`)** | `planner`는 읽기 전용, `reviewer`는 파일 수정 불가 등 역할 통제 | **항상 활성 (차단)** |
| **4. 무한 루프 방지 (`MAX_ORCHESTRATOR_STEPS`, `MAX_SUBAGENT_STEPS`)** | 오케스트레이터(8회), 서브 에이전트(6회) 최대 스텝 제한 | **항상 활성 (중단)** |

---

## 3. 실행 시나리오별 CLI 사용법

### 시나리오 A: 완전 자동화 샌드박스 실행 (추천)
별도의 `./sandbox` 폴더를 자동 생성하여 격리하고, 모든 코드 작성과 실행 테스트를 사용자 개입 없이 한 번에 진행합니다.
```powershell
python .\multi_agent_loop-v2.py --auto --workspace .\sandbox
```

### 시나리오 B: 격리만 적용하고 단계별 수동 승인 유지
작업 영역은 `./project_test` 폴더로 제한하되, 파일 저장과 PowerShell 실행 시 직접 `y/n`으로 검토하며 진행합니다.
```powershell
python .\multi_agent_loop-v2.py --workspace .\project_test
```

### 시나리오 C: 현재 디렉터리 기준 자동 실행
기존 작업 폴더에서 승인 절차만 생략하고 빠르게 작업을 수행합니다. (단, 기존 파일 덮어쓰기에 주의 필요)
```powershell
python .\multi_agent_loop-v2.py --auto
```

---

## 4. 내부 실행 흐름 요약 (`--auto --workspace ./sandbox` 기준)

```text
[사용자 실행] python multi_agent_loop-v2.py --auto --workspace ./sandbox
       │
       ├─ 1. 초기화: ./sandbox 디렉터리 자동 생성 및 절대 경로 확정
       ├─ 2. 오케스트레이터: 사용자 지시 분석 후 Planner 위임
       ├─ 3. Planner: ./sandbox 내부 파일 구조만 읽기 전용으로 탐색
       ├─ 4. Worker: 
       │     ├─ write_file("tetris.py") → 경로 검증 통과 → [AUTO] 즉시 저장
       │     └─ run_powershell("python tetris.py") → cwd=./sandbox 고정 → [AUTO] 즉시 실행
       ├─ 5. Reviewer: ./sandbox 내 결과물 검증 및 PASS/FAIL 보고
       └─ 6. 오케스트레이터: 최종 결과 한국어 요약 출력
```

---

## 5. 운영 시 권장 보안 팁
1. **`--auto` 사용 시 반드시 `--workspace`와 함께 사용하세요.** 소형 LLM(4B급)은 간혹 파일 경로를 오인하여 기존 소스를 덮어쓸 수 있으므로, 독립된 하위 폴더를 지정하는 것이 가장 안전합니다.
2. **네트워크나 외부 시스템 제어 스크립트 작성 시:** `DANGEROUS_PATTERNS`는 로컬 디스크·전원 파괴 명령을 중심으로 차단하므로, 외부 API 호출이나 메일 발송 같은 스크립트를 테스트할 때는 수동 승인 모드(기본값)를 권장합니다.
