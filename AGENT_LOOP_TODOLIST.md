현재까지 구현된 agent_loop.py 기준으로 앞서 작성한 6단계 To-Do 리스트의 완료([x]) 및 미완료([ ]) 항목을 체크한 결과입니다.

## 📋 코딩 에이전트 구축 To-Do List 현황 체크


### 1단계: 기본 환경 및 두뇌(LLM) 연결 — ✅ 완료

• 로컬 LLM 엔진 세팅: llama-server.exe로 qwen2.5-coder-3b-instruct-q4_k_m.gguf 모델 서빙 완료 (--jinja, -c 16384)
• Python 프로젝트 초기화: C:\Users\fall2\work\simple-coding-agent 작업 환경 구성 및 openai 라이브러리 설치 완료
• API 클라이언트 연동: OpenAI(base_url="http://127.0.0", api_key="not-needed")로 로컬 모델 연결 완료
• 추론 파라미터 고정: JSON 포맷 파손 및 환각 방지를 위해 temperature=0.2 설정 완료

### 2단계: 필수 도구(Tools) 8종 구현 및 스키마 정의 — ✅ 완료

• 탐색 도구 구현 (list_dir, glob, grep) 구현 완료:
	• list_dir: 특정 디렉토리 내 파일/폴더 목록 반환 (디렉토리는 '/' 접미사, 최대 200개)
	• glob: 확장자나 패턴(**/*.py)으로 파일 경로 검색 (recursive, 최대 200개)
	• grep: 코드 내 특정 함수명이나 키워드가 포함된 파일과 라인 번호 검색 (정규식, include 필터, 최대 120건)
• 파일 조작 도구 구현 (read_file, write_file, edit_file):
	• read_file: 파일 내용 읽기 구현 완료
	• write_file: 새 파일 생성 및 전체 덮어쓰기 구현 완료
	• edit_file: 전체 덮어쓰기 대신 특정 기존 문자열(old_str)을 새 문자열(new_str)로 부분 치환 구현 완료 (미존재/중복 매칭 시 에러 피드백, 사용자 승인 포함)
• 터미널 도구 구현 (run_powershell): Windows PowerShell 실행, UTF-8 인코딩 강제 및 timeout=30 설정 완료
• 도구 라우터 작성: execute_tool() 디스패처 및 텍스트 JSON 폴백 파서(extract_fallback_tool_calls) 구현 완료

### 3단계: 안전장치(Human-in-the-loop) 및 예외 처리 — ✅ 완료

• 실행 전 사용자 승인 로직 추가: write_file, run_powershell 실행 직전 터미널에서 [y/n] 승인 대기 구현 완료
• JSON 파싱 방어 코드 작성: 잘못된 JSON 생성 시 에러 메시지를 role: "tool"로 피드백해 자가 수정 유도 구현 완료
• 위험 명령어 필터링: Remove-Item -Recurse -Force, Format-Volume, Clear-Disk, diskpart, shutdown 등 치명적 명령어 사전 차단 리스트 구현 완료 (승인 프롬프트 이전에 차단)

### 4단계: 에이전트 루프(Agentic Loop) 구축 — ✅ 완료

• 외부 대화 루프 구현: 사용자 터미널 입력 수신 및 messages 누적 루프 구현 완료
• 내부 추론-실행 루프 구현: 도구 호출이 없을 때까지 role: "tool" 결과를 재전달하는 내부 while 루프 구현 완료
• 무한 루프 방지 장치(max_steps): 한 질문당 최대 8회(MAX_STEPS = 8) 도구 호출 제한 구현 완료

### 5단계: 컨텍스트 관리(Context Management) 고도화 — ✅ 완료

• 프로젝트 지침 파일(AGENT.md 또는 CLAUDE.md) 자동 로드: 현재 폴더의 AGENT.md, CLAUDE.md, README.md, SETUP.md를 시작 시 자동 읽어 시스템 프롬프트에 주입 구현 완료 (파일당 1,200자 / 전체 3,000자 제한)
• 작업 행동 강령 프롬프트 작성: 현재 작업 디렉토리(os.getcwd()) 주입 및 Few-shot JSON 강제 프롬프트 작성 완료
• 토큰/대화 길이 관리: trim_context 슬라이딩 윈도우(MAX_HISTORY = 24) 및 도구 출력 길이 제한(MAX_TOOL_OUTPUT = 6000) 구현 완료

### 6단계: 통합 테스트 및 검증 — 🔄 진행 중 (2/3 완료)

• 시나리오 1 (탐색 및 이해): 현재 디렉토리 파일 목록 조회 테스트 성공
• 시나리오 2 (생성 및 실행): 간단한 계산기 모듈과 테스트 코드를 만들고 실행 검증 테스트
• 시나리오 3 (부분 수정): edit_file 도구를 이용한 기존 코드 부분 수정 테스트 완료 — `test_edit_file.py` (도구 계층 8/8 통과) 및 `test_live_llm.py` (실제 LLM API 호출 5/5 통과, 요청/응답·타임스탬프를 `test_results/`에 텍스트로 저장)

### 💡 현재 요약

• 핵심 뼈대(1단계, 4단계)와 필수 도구 8종(2단계)이 완성되어 실제 로컬 에이전트로 동작 중입니다.
• 시나리오 3(edit_file 부분 수정)을 도구 계층 8/8 및 실제 LLM API 호출 5/5로 검증했고, 남은 과제는 시나리오 2(생성 및 실행 검증)입니다.
다음으로 진행할 항목을 선택해 주시겠어요?
• 시나리오 2 (간단한 계산기 모듈 생성 후 실행 검증) 통합 테스트
