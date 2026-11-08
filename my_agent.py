import os
import re
import sys
import json
import subprocess
from openai import OpenAI

# 로컬 통신 시 프록시 우회 설정
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["no_proxy"] = "localhost,127.0.0.1"

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="local-key"
)

def safe_decode(b: bytes) -> str:
    """Windows PowerShell 출력(UTF-8 / CP949) 안전 디코딩"""
    if not b:
        return ""
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return b.decode("cp949", errors="replace").strip()

def parse_agent_response(text: str) -> dict:
    """
    <think> 제거 후 JSON 객체를 파싱하며,
    'write' 액션일 경우 JSON 뒤에 붙은 ```python ... ``` 블록을 content로 자동 추출합니다.
    """
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL).strip()

    # JSON 객체 추출 (첫 번째 { ... } 블록)
    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if not json_match:
        raise ValueError("응답에서 JSON 객체를 찾을 수 없습니다.")

    data = json.loads(json_match.group(0), strict=False)

    # write 액션인데 content가 비어있거나 뒤에 코드 블록이 붙어있는 경우 본문 코드 추출
    if data.get("action") == "write":
        code_match = re.search(r'```[a-zA-Z]*\n(.*?)```', text, re.DOTALL)
        if code_match:
            data["content"] = code_match.group(1)

    return data

def read_text_file(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except UnicodeDecodeError:
        with open(filepath, "r", encoding="cp949") as f:
            return f.read().strip()

system_prompt = """/no_think
당신은 Windows 환경에서 동작하는 코딩 및 시스템 자동화 AI 에이전트입니다.
반드시 아래 3가지 형식 중 하나로만 응답하세요.

1. 파일 생성 (코드 작성 시 반드시 이 형식을 가장 먼저 사용하세요. JSON 뒤에 실제 코드를 ``` 코드블록으로 붙이세요):
{"action": "write", "file": "파일이름.py"}
```python
여기에 전체 파이썬 코드를 작성 (JSON 이스케이프 불필요)
```

2. 명령 실행 (작성된 .py 파일 실행 또는 시스템 명령):
{"action": "shell", "command": "python 파일이름.py"}

3. 사용자에게 답변 및 최종 결과 보고:
{"action": "reply", "message": "사용자에게 전달할 한국어 답변"}

[필수 행동 수칙]
- Python Windows GUI 앱(게임, 계산기 등) 요청 시 별도 설치가 필요 없는 Python 기본 내장 `tkinter` 모듈을 사용하여 완성도 높은 코드를 작성하세요.
- 코드를 실행할 때는 절대 한 줄 명령(`python -c`)이나 C# 컴파일을 쓰지 말고, 반드시 1단계에서 `{"action": "write", "file": "tetris.py"}`로 파일을 먼저 저장한 뒤, 2단계에서 `{"action": "shell", "command": "python tetris.py"}`로 실행하세요.
- 단순 질문은 도구를 쓰지 말고 즉시 `{"action": "reply", "message": "..."}`로 답변하세요."""

print("🤖 범용 자율 AI 에이전트가 시작되었습니다. (종료: exit)")
print("💡 일반 질문을 입력하거나, 지시사항이 적힌 .txt 파일 경로를 입력하세요.")
print("=" * 75)

while True:
    try:
        user_input = input("\n🗣️ [입력 또는 파일 경로]: ").strip()
    except (KeyboardInterrupt, EOFError):
        break

    if not user_input:
        continue
    if user_input.lower() in ["exit", "quit", "종료"]:
        break

    file_match = re.search(r'["\']?([a-zA-Z]:\\[^"\'\s]+\.txt\(\vert{}\S+\.\)txt)["\']?', user_input)
    clean_path = file_match.group(1) if file_match else user_input.strip('"\'')

    if os.path.isfile(clean_path):
        try:
            file_content = read_text_file(clean_path)
            if not file_content:
                print("⚠️ [경고]: 선택한 파일이 비어 있습니다.")
                continue
            print(f"📄 [파일 지시서 로드 완료]: '{clean_path}' ({len(file_content)}자)")
            user_prompt = file_content if user_input.strip('"\'') == clean_path else f"{user_input}\n\n[파일 내용 ({clean_path})]:\n{file_content}"
        except Exception as e:
            print(f"❌ [파일 읽기 실패]: {e}")
            continue
    else:
        user_prompt = user_input

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    preview = user_prompt.replace("\n", " ")
    print(f"🎯 [요청 처리]: {preview[:100]}{'...' if len(preview) > 100 else ''}\n" + "-" * 50)

    max_steps = 7
    for step in range(1, max_steps + 1):
        print(f"\n🔄 [Step {step}/{max_steps}] 생각 중...")

        response = client.chat.completions.create(
            model="local-model",
            messages=messages,
            temperature=0.2,
            max_tokens=4096,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )

        raw_answer = response.choices[0].message.content.strip()
        messages.append({"role": "assistant", "content": raw_answer})

        try:
            data = parse_agent_response(raw_answer)
            action = data.get("action")
        except Exception as e:
            print(f"⚠️ [파싱 실패]: {e}\n원본 응답 일부: {raw_answer[:120]}...")
            messages.append({
                "role": "user",
                "content": f"[형식 에러]: {e}\n반드시 지정된 JSON 형식으로 다시 출력하세요. 파일 작성 시 JSON 뒤에 ```python 코드블록을 붙이세요. /no_think"
            })
            continue

        # 1) write 액션
        if action == "write":
            fpath = data.get("file", "").strip()
            content = data.get("content", "")

            if not fpath or not content:
                messages.append({"role": "user", "content": "[에러]: 파일 경로 또는 코드 블록 내용이 비어 있습니다. JSON 뒤에 ```python 코드블록을 포함해 다시 출력하세요. /no_think"})
                continue

            dir_name = os.path.dirname(fpath)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

            print(f"📝 [파일 작성]: '{fpath}' ({len(content.splitlines())}줄)")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

            messages.append({
                "role": "user",
                "content": f"[시스템 알림]: '{fpath}' 저장이 완료되었습니다. 이제 실행이 필요하면 {{\"action\": \"shell\", \"command\": \"python {fpath}\"}} 를 출력하세요. /no_think"
            })

        # 2) shell 액션
        elif action == "shell":
            cmd = data.get("command", "").strip()
            print(f"💻 [명령 실행]: {cmd}")

            # python 파일 실행 명령인 경우 권한 에러 방지를 위해 현재 파이썬 인터프리터(sys.executable)로 직접 실행
            py_match = re.match(r'^(?:Start-Process\s+)?python(?:\.exe)?\s+["\']?([^\s"\']+\.py)["\']?$', cmd, re.IGNORECASE)
            if py_match:
                target_py = py_match.group(1)
                try:
                    # 2초간 문법/초기화 에러가 나는지 감시 후, GUI 창이 유지되면 백그라운드 실행으로 인정
                    proc = subprocess.Popen(
                        [sys.executable, target_py],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace"
                    )
                    try:
                        out, err = proc.communicate(timeout=2.5)
                        if proc.returncode != 0:
                            print(f"❌ [실행 에러]:\n{err}")
                            messages.append({"role": "user", "content": f"[실행 에러]:\n{err}\n코드를 수정하여 다시 write 하세요. /no_think"})
                        else:
                            out_text = out.strip() or "(출력 없이 정상 종료됨)"
                            print(f"👀 [실행 결과]: {out_text}")
                            messages.append({"role": "user", "content": f"[실행 결과]: {out_text}\n완료되었다면 'reply'로 사용자에게 보고하세요. /no_think"})
                    except subprocess.TimeoutExpired:
                        print(f"🪟 [GUI 앱 실행 완료]: '{target_py}' 창이 화면에 실행되었습니다.")
                        messages.append({"role": "user", "content": f"[시스템 알림]: '{target_py}' GUI 프로그램이 정상 실행되었습니다. 이제 'reply' 액션으로 사용자에게 조작법과 완료 보고를 하세요. /no_think"})
                except Exception as e:
                    print(f"❌ [실행 실패]: {e}")
                    messages.append({"role": "user", "content": f"[실행 실패]: {e}\n다시 시도하세요. /no_think"})
                continue

            # 일반 PowerShell 명령 실행
            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", utf8_cmd],
                    capture_output=True, shell=False, timeout=15
                )
                stdout_str = safe_decode(res.stdout)
                stderr_str = safe_decode(res.stderr)

                if res.returncode != 0 or stderr_str:
                    err = stderr_str or stdout_str
                    print(f"❌ [실행 에러]:\n{err}")
                    messages.append({"role": "user", "content": f"[에러 발생]:\n{err}\n원인을 파악해 수정 후 다시 시도하세요. /no_think"})
                else:
                    out = stdout_str or "(출력 없이 정상 실행됨)"
                    print(f"👀 [실행 결과]: {out}")
                    messages.append({"role": "user", "content": f"[실행 결과]: {out}\n완료되었다면 'reply'로 사용자에게 답변하세요. /no_think"})
            except subprocess.TimeoutExpired:
                print("🪟 [백그라운드 실행 감지]: 프로세스가 백그라운드에서 유지되고 있습니다.")
                messages.append({"role": "user", "content": "[시스템 알림]: 백그라운드에서 정상 실행되었습니다. 'reply'로 완료 보고하세요. /no_think"})
            except Exception as e:
                print(f"❌ [명령 실행 예외]: {e}")
                messages.append({"role": "user", "content": f"[시스템 에러]: {e}\n다른 방법으로 시도하세요. /no_think"})

        # 3) reply 액션
        elif action == "reply":
            print("\n💬 [에이전트 답변]:")
            print(data.get("message", ""))
            break

        else:
            print(f"⚠️ [알 수 없는 액션]: {data}")
            messages.append({"role": "user", "content": "[에러]: 'action' 값은 반드시 'write', 'shell', 'reply' 중 하나여야 합니다. /no_think"})
    else:
        print("\n⚠️ [안내]: 최대 실행 단계(7단계)에 도달하여 작업을 중단했습니다.")
