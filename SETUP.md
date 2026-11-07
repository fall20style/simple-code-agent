# Intel Arc GPU 기반 로컬 LLM & 코딩 에이전트 환경 재설치 가이드 (SETUP.md)

본 문서는 Windows + Intel Arc 외장 GPU(VRAM 4GB) 환경에서 불필요하거나 버전이 충돌하는 기존 Python 패키지를 정리하고, 공식 Vulkan 기반 `llama.cpp` 서버 및 커스텀 코딩 에이전트(`my_agent.py`)를 가장 안정적으로 재구성하기 위한 설정 가이드입니다.

---

## 1. 기존 충돌 패키지 및 빌드 환경 변수 초기화

그동안 테스트 과정에서 설치된 CPU 전용 `llama-cpp-python` 및 런타임 DLL 버전 충돌(`Exit Code -1073741515`)을 일으키던 패키지들을 깨끗하게 제거합니다.

```powershell
# 1. C++ 수동 컴파일용 임시 환경 변수 초기화
Remove-Item Env:CMAKE_ARGS -ErrorAction SilentlyContinue
Remove-Item Env:FORCE_CMAKE -ErrorAction SilentlyContinue

# 2. 충돌 및 불필요 패키지 일괄 삭제
pip uninstall -y llama-cpp-python ipex-llm bigdl-cpp dpcpp-cpp-rt mkl-dpcpp
```

---

## 2. 코딩 에이전트 구동용 필수 Python 패키지 설치

무거운 C++ 빌드 도구 없이, 로컬 Vulkan API 서버(`llama-server.exe`)와 통신할 공식 `openai` 클라이언트 패키지 및 기본 도구를 설치합니다.

```powershell
# pip 최신 버전 업데이트
python -m pip install --upgrade pip

# 로컬 API 서버 연동용 필수 패키지 설치
pip install openai
```

---

## 3. (선택 사항) Intel Arc GPU(XPU) 직접 연산용 PyTorch 설치

API 서버 방식(`llama-server.exe`) 외에, 향후 Python 코드 내부에서 PyTorch로 Intel Arc GPU(`xpu:0`)를 직접 제어하고 싶다면 인텔 공식 XPU 전용 빌드를 추가로 설치합니다.

```powershell
# Windows용 Intel Arc GPU(XPU) 가속 PyTorch 설치
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/xpu
```

* **설치 검증 명령어:**
  ```powershell
  python -c "import torch; print('XPU Available:', torch.xpu.is_available()); print('Device:', torch.xpu.get_device_name(0) if torch.xpu.is_available() else 'None')"
  ```

---

## 4. 백엔드 서버(`llama.cpp` Vulkan) 및 모델 준비

1. **서버 바이너리 다운로드:** [llama.cpp 공식 GitHub Releases](https://github.com/ggml-org/llama.cpp/releases)에서 `llama-bXXXX-bin-win-vulkan-x64.zip` 파일을 내려받아 프로젝트 루트 폴더에 압축을 해제합니다.
2. **양자화 모델 배치:** `qwen2.5-coder-3b-instruct-q4_k_m.gguf` 파일을 프로젝트 루트 폴더에 위치시킵니다.
3. **인텔 외장 GPU 디바이스 번호 확인:**
   ```powershell
   .\llama-server.exe --list-devices
   ```
   *(Windows 작업 관리자의 `GPU 0`인 Intel Arc 외장 그래픽이 Vulkan 목록에서 `Vulkan1`로 매핑되어 있는지 확인합니다.)*

---

## 5. 최종 실행 및 연동 체크리스트

환경 정리가 완료되면 두 개의 PowerShell 터미널을 열어 순서대로 실행합니다.

### [터미널 1] 인텔 GPU 가속 API 서버 실행
```powershell
.\llama-server.exe -m "./qwen2.5-coder-3b-instruct-q4_k_m.gguf" --device Vulkan1 --jinja -ngl 14 -c 16384 --temp 0.2 --top-p 0.8 --min-p 0.05 --repeat-penalty 1.15 --host 127.0.0.1 --port 8000
```

### [터미널 2] 커스텀 Python 코딩 에이전트 실행
```powershell
python .\my_agent.py
```

### 정상 작동 확인 포인트
* **작업 관리자 (`Ctrl + Shift + Esc`):** 성능 탭에서 **GPU 0 (Intel Arc)**의 전용 GPU 메모리 점유율(약 2.5~3.5GB) 및 Compute/3D 그래프 상승 확인.
* **네트워크 트래픽:** 외부 인터넷 통신 없이 로컬 루프백(`127.0.0.1:8000`)으로만 추론 요청 및 응답 처리 확인.
