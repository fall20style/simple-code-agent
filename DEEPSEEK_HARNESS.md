# DeepSeek Harness (`dsh`) 로컬 구축 및 연동 정리

## 1. 개요
- **목적:** 오픈소스 에이전트 런타임인 **DeepSeek Harness (`dsh`)**를 Windows 로컬 환경에 직접 빌드·구동하고, 로컬 인텔 GPU API 서버(`llama-server.exe`)와 연동하여 코딩 에이전트 동작을 검증함.
- **저장소:** `https://github.com/deepseek-ai/deepseek-harness.git`

---

## 2. 환경 구축 및 빌드 과정 (Node.js 없는 환경 우회)

Windows 시스템에 `node` 및 `npm`이 설치되어 있지 않은 상태에서 독립형 `pnpm`을 활용해 런타임과 빌드를 구성함.

### 1단계: 저장소 클론 및 독립형 `pnpm` 설치
```powershell
# 1. 저장소 복제 및 이동
git clone https://github.com/deepseek-ai/deepseek-harness.git
cd deepseek-harness

# 2. PowerShell 독립 스크립트로 pnpm 설치
Invoke-WebRequest https://get.pnpm.io/install.ps1 -UseBasicParsing | Invoke-Expression

# 3. 현재 터미널 세션의 환경 변수(Path) 즉시 갱신
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + [System.Environment]::GetEnvironmentVariable("Path","Machine")
```

### 2단계: Node.js LTS 런타임 설치 및 프로젝트 빌드
```powershell
# 1. pnpm 자체 기능으로 Node.js LTS 버전 설치 및 전역 활성화
pnpm env use --global lts

# 2. 의존성 패키지 설치 및 소스 코드 빌드
pnpm install
pnpm run build
```

---

## 3. 웹 대시보드(`dsh web`) 구동 및 접속

### 실행 명령어
```powershell
pnpm dsh web --host 127.0.0.1 --port 3080
```
- **참고 (CLI 모드 실행 시):** `pnpm dsh` 단독 실행 시 `--profile <name>` 인자가 필수이므로, 웹 UI 모드(`pnpm dsh web`)를 사용하는 것이 직관적임.
- **접속 주의사항:** Windows 권한(`spawn EPERM`)으로 브라우저 자동 실행이 생략될 수 있으므로, 터미널에 출력되는 **보안 토큰(`?token=...`)이 포함된 전체 URL**을 복사해 브라우저로 접속해야 함.
  - 예시: `http://127.0.0.1:3080/?token=<생성된_토큰값>`

---

## 4. 로컬 LLM API 서버(`llama-server.exe`) 연동 설정

### 1단계: 로컬 API 서버 요구사항 조정
- DeepSeek Harness가 전송하는 기본 시스템 프롬프트(에이전트 툴 스키마 포함)가 약 **9,908 토큰**에 달하므로, 기존 컨텍스트(`1280`)에서는 `ValueError: Requested tokens exceed context window` 에러가 발생함.
- 이를 수용하기 위해 로컬 서버의 컨텍스트 윈도우를 **`16384`**로 확장하고 `--jinja` 템플릿 옵션을 활성화하여 가동함.

```powershell
.\llama-server.exe -m "./qwen2.5-coder-3b-instruct-q4_k_m.gguf" --device Vulkan1 --jinja -ngl 14 -c 16384 --temp 0.2 --top-p 0.8 --min-p 0.05 --repeat-penalty 1.15 --host 127.0.0.1 --port 8000
```

### 2단계: Web UI (`Settings -> Models`) 프로바이더 등록
1. 좌측 하단 **Settings -> Models** 진입 후 **Add Provider** 클릭.
2. 아래와 같이 커스텀 로컬 서버 정보 입력 후 저장:
   - **Provider Type:** `OpenAI Compatible`
   - **Base URL:** `http://127.0.0.1:8000/v1`
   - **API Key:** `local-key` (더미 키)
   - **Model ID:** `local-model`
3. 메인 화면에서 **Choose workspace**를 눌러 작업 폴더 지정 후 모델 선택.

---

## 5. 연동 테스트 결과 및 확인된 한계점

1. **정상 동작 확인:**
   - 토큰이 포함된 Web UI 접속 및 로컬 API 서버(`8000` 포트) 연결 성공.
   - 초기 프롬프트(약 10,070 토큰) 처리 이후 프롬프트 캐싱(`LCP similarity`)이 동작하여 후속 응답 속도 안정화.
   - 샘플링 제어(`--temp 0.2 --repeat-penalty 1.15`) 적용 후 경량 모델의 중국어 학습 데이터 환각 현상 억제 확인.

2. **3B 경량 모델 연동 시 한계:**
   - **Tool Calling 태그 누락:** `Qwen2.5-Coder-3B` 모델이 `{"name": "write", "arguments": {...}}` 형태의 JSON 데이터는 올바르게 생성하나, 정식 `<tool_call>` 태그를 생략한 채 일반 텍스트로 출력하여 하네스가 실제 파일 쓰기 도구를 트리거하지 못하는 경우가 발생함.
   - **컨텍스트 부하 및 추론 한계:** 10,000토큰이 넘는 무거운 시스템 프롬프트로 인해 3B 모델이 존재하지 않는 파일(`.DSHiles.txt`)을 읽으려 시도하는 등 도구 선택 정확도가 떨어짐.
   - **결론:** DeepSeek Harness는 `DeepSeek-V3/R1` 등 대형 모델이나 상용 API에 최적화되어 있으며, 4GB VRAM + 3B 로컬 모델 환경에서는 초경량 프롬프트 기반의 커스텀 에이전트 루프를 사용하는 것이 훨씬 효율적임.
