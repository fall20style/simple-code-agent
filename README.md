# 🤖 Intel Arc GPU 기반 완전 로컬 자율 코딩 에이전트 (Local Coding Agent)

외부 클라우드 API나 인터넷 연결 없이, **인텔 아크 외장 그래픽(Intel Arc A350M, VRAM 4GB)** 환경에서 경량 코딩 특화 모델(**Qwen2.5-Coder-3B**)을 로컬 서빙하고, 스스로 코드를 작성·실행·수정하는 **자율 코딩 에이전트(Autonomous Coding Agent)** 구축 과제입니다.

---

## 📌 1. 과제 개요 및 목표
- **완전한 오프라인 프라이버시 보장:** 소스코드나 시스템 데이터가 외부로 유출되지 않는 100% 로컬 추론 환경 구축
- **저사양 VRAM(4GB) 하드웨어 한계 극복:** 레이어 분할 적재(Offloading)와 Vulkan 백엔드 가속을 통해 3B 파라미터 모델을 안정적으로 서빙
- **경량 모델 최적화 에이전트 아키텍처 구현:** 무거운 상용 하네스(10,000+ 토큰) 대신, 3B 모델의 추론 정확도와 속도를 극대화하는 초경량(약 250 토큰) 맞춤형 ReAct Agent Loop 개발

---

## 🏗️ 2. 시스템 아키텍처

```text
[사용자 (CLI 터미널)]
       │  자연어 작업 지시 (예: "구구단 3단 GUI 프로그램 작성 및 실행")
       ▼
[Python Agent Loop (my_agent.py)] ◄──► [Windows PowerShell / 파일 시스템]
       │  OpenAI 호환 REST API (http://127.0.0.1:8000/v1)
       ▼
[llama.cpp Vulkan Server (llama-server.exe)]
       │  하이브리드 레이어 분할 적재 (-ngl 14)
       ├──► Intel Arc A350M (GPU 0 / Vulkan1, 전용 VRAM 4GB)
       └──► System RAM (16GB, 잔여 레이어 및 KV 캐시 분산 처리)
```

---

## 💻 3. 개발 및 실행 환경

| 구분 | 사양 및 도구 | 비고 |
| :--- | :--- | :--- |
| **OS** | Windows 11 (x64) | PowerShell 기반 로컬 셸 제어 |
| **GPU** | Intel Arc A350M (전용 VRAM 4GB) | Vulkan 백엔드(`Vulkan1`) 매핑 |
| **RAM / CPU** | 16GB RAM / 12 Threads | 하이브리드 오프로딩 지원 |
| **LLM 모델** | `Qwen2.5-Coder-3B-Instruct` (`Q4_K_M`) | 약 2.2GB, 코딩 특화 양자화 모델 |
| **추론 엔진** | `llama.cpp` (Windows Vulkan Binary) | OpenAI 호환 API 서버 구동 |
| **에이전트 코어** | Python 3.11 + `openai` SDK | 커스텀 XML 파싱 및 자가 수정 루프 |

---

## ✨ 4. 핵심 구현 기능

1. **인텔 외장 GPU 타겟팅 및 메모리 최적화**
   - 내장 그래픽(`GPU 1` 공유 메모리)이 아닌 실제 외장 그래픽(`GPU 0` 전용 VRAM)을 `--device Vulkan1` 옵션으로 강제 지정
   - `-ngl 14` 설정으로 36개 모델 레이어 중 14개를 VRAM에 적재하여 `Out of Memory(OOM)` 방지
2. **XML 태그 기반 안정적 도구 파싱 (`<write>`, `<shell>`)**
   - 경량 모델이 긴 파이썬/GUI 코드를 JSON 문자열로 출력할 때 발생하는 따옴표·줄바꿈 이스케이프 오류(`Invalid control character`)를 방지하기 위해 직관적인 XML 태그 파싱 방식 도입
3. **자가 수정(Self-Correction) 및 루프 감시자(Guardrail)**
   - 명령어 실행 실패(`returncode != 0` 또는 `stderr` 발생) 시 에러 로그를 모델에게 자동 피드백하여 스스로 코드를 수정하고 재실행하도록 유도
   - 에러가 해결되지 않은 상태에서 에이전트가 조기 종료를 시도할 경우 루프 감시자가 이를 차단하고 재작업을 강제
4. **Windows 환경 특화 예외 처리**
   - **인코딩 자동 변환:** 한글 윈도우 PowerShell의 `CP949` 출력과 `UTF-8` 충돌을 방지하는 바이너리 안전 디코딩(`safe_decode`) 적용
   - **GUI 비동기 실행 지원:** `tkinter` 등 GUI 애플리케이션 실행 시 창 유지로 인한 타임아웃(`TimeoutExpired`)을 정상 실행 완료 상태로 처리

---

## 🚀 5. 설치 및 실행 가이드

### 1단계: 사전 준비
1. [llama.cpp 공식 릴리즈](https://github.com/ggml-org/llama.cpp/releases)에서 Windows Vulkan 바이너리(`bin-win-vulkan-x64.zip`)를 다운로드하여 작업 폴더에 압축을 풉니다.
2. `qwen2.5-coder-3b-instruct-q4_k_m.gguf` 모델 파일을 동일 폴더에 배치합니다.
3. 파이썬 필수 패키지를 설치합니다.
   ```powershell
   pip install openai
   ```

### 2단계: 로컬 LLM API 서버 구동 (터미널 1)
인텔 아크 외장 GPU 가속과 환각 억제 샘플링 옵션이 적용된 서버를 백그라운드에서 실행합니다.
```powershell
.\llama-server.exe -m "./qwen2.5-coder-3b-instruct-q4_k_m.gguf" --device Vulkan1 --jinja -ngl 14 -c 16384 --temp 0.2 --top-p 0.8 --min-p 0.05 --repeat-penalty 1.15 --host 127.0.0.1 --port 8000
```

### 3단계: 자율 코딩 에이전트 실행 (터미널 2)
새 터미널 창을 열고 에이전트 스크립트를 실행한 뒤, 자연어로 원하는 작업을 지시합니다.
```powershell
python .\my_agent.py
```

#### 💬 실행 예시
```text
🤖 안정화 코딩 에이전트가 시작되었습니다. (종료: exit)
=================================================================

🗣️ [명령 입력]: 구구단 3단을 출력하는 Python 기반 GUI 애플리케이션을 작성하고 실행해 줘
🎯 [작업 시작]: 구구단 3단을 출력하는 Python 기반 GUI 애플리케이션을 작성하고 실행해 줘
--------------------------------------------------

🔄 [Step 1/5] 추론 중...
📝 [파일 작성]: 'gugudan3_gui.py' 생성 중...

🔄 [Step 2/5] 추론 중...
💻 [명령 실행]: Start-Process python gugudan3_gui.py
👀 [실행 결과]: (실행 완료)

✅ [최종 에이전트 보고]:
구구단 3단을 출력하는 GUI 프로그램(gugudan3_gui.py)을 작성하고 실행했습니다.
```

---

## 📂 6. 프로젝트 파일 구성
- `my_agent.py`: 대화형 입력, XML 도구 파싱, 자가 수정 루프가 구현된 메인 코딩 에이전트 스크립트
- `HISTORY.md`: 하드웨어 분석, 엔진 전환(Ollama → IPEX-LLM → Vulkan), 트러블슈팅 과정을 기록한 기술 일지
- `README.md`: 과제 개요, 시스템 구조, 실행 방법을 정리한 프로젝트 소개 문서
