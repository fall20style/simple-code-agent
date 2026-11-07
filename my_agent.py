import os
import re
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

def extract_json(text: str) -> dict:
    """마크다운 코드블록(```json)이 섞여 있어도 순수 JSON 객체만 안전하게 파싱"""
    text = text.strip()
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]
    return json.loads(text)

def read_text_file(filepath: str) -> str:
    """UTF-8 및 Windows CP949 인코딩을 모두 지원하는 텍스트 파일 읽기"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except UnicodeDecodeError:
        with open(filepath, "r", encoding="cp949") as f:
            return f.read().strip()

# 특정 작업에 한정하지 않은 범용 자율 에이전트 시스템 프롬프트
system_prompt = """당신은 사용자의 일반 질문에 답변하고, 코딩, 파일 관리, 시스템 자동화 등 다양한 작업을 수행하는 범용 AI 에이전트입니다.
모든 응답은 반드시 아래 3가지 JSON 형식 중 하나만 출력하세요. JSON 외의 일반 텍스트나 설명은 절대 포함하지 마세요.

1. 파일 생성 및 내용 덮어쓰기 (코드 작성, 문서/설정 파일 생성 등):
{
  "action": "write",
  "file": "경로/파일이름.확장자",
  "content": "파일에 들어갈 전체 내용 (줄바꿈은 \\n, 내부 큰따옴표는 \\" 로 이스케이프)"
}

2. PowerShell 명령 실행 (스크립트 실행, 폴더/파일 이동·삭제·조회, 시스템 자동화 등):
{
  "action": "shell",
  "command": "실행할 PowerShell 명령어"
}

3. 사용자에게 답변 및 최종 결과 보고 (일반 질문 답변, 모든 작업 완료 후 보고):
{
  "action": "reply",
  "message": "사용자에게 전달할 한국어 답변 또는 작업 완료 보고"
}

[판단 및 행동 수칙]
- 단순 지식 질문, 대화, 아이디어 요청 등 PC 작업이 필요 없는 요청은 도구를 쓰지 말고 즉시 "action": "reply"로 성실히 답변하세요.
- 코딩, 파일 정리, 시스템 제어 등 실제 실행이 필요한 요청은 "write"나 "shell"을 단계적으로 사용해 직접 처리한 뒤, 마지막에 "action": "reply"로 결과를 보고하세요.
- GUI 프로그램(tkinter 등)을 실행할 때는 터미널이 멈추지 않도록 "command": "Start-Process python 파일이름.py" 를 사용하세요."""

print("🤖 범용 자율 AI 에이전트가 시작되었습니다. (종료: exit)")
print("💡 일반 질문을 입력하거나, 지시사항이 적힌 .txt 파일 경로(또는 포함된 문장)를 입력하세요.")
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

    # 문장 안에 따옴표로 감싸졌거나 공백으로 구분된 실제 .txt 파일 경로가 있는지 자동 추출
    file_match = re.search(r'["\']?([a-zA-Z]:\\[^"\'\s]+\.txt|\S+\.txt)["\']?', user_input)
    clean_path = file_match.group(1) if file_match else user_input.strip('"\'')

    if os.path.isfile(clean_path):
        try:
            file_content = read_text_file(clean_path)
            if not file_content:
                print("⚠️ [경고]: 선택한 파일이 비어 있습니다.")
                continue
            print(f"📄 [파일 지시서 로드 완료]: '{clean_path}' ({len(file_content)}자)")
            
            # 경로만 입력했으면 파일 내용 그대로, 추가 지시사항 문장이 섞여 있으면 함께 전달
            if user_input.strip('"\'') == clean_path:
                user_prompt = file_content
            else:
                user_prompt = f"{user_input}\n\n[파일 내용 ({clean_path})]:\n{file_content}"
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
            max_tokens=1024,
            response_format={"type": "json_object"}
        )

        raw_answer = response.choices[0].message.content.strip()
        messages.append({"role": "assistant", "content": raw_answer})

        try:
            data = extract_json(raw_answer)
            action = data.get("action")
        except json.JSONDecodeError as e:
            print(f"⚠️ [JSON 파싱 실패]: {e}")
            messages.append({
                "role": "user",
                "content": f"[JSON 파싱 에러]: 유효한 JSON 형식이 아닙니다 ({e}). 이스케이프(\\, \\n, \\\")를 정확히 지켜 순수 JSON 객체만 다시 출력하세요."
            })
            continue

        # 1) write 액션 (파일 생성/수정)
        if action == "write":
            fpath = data.get("file", "").strip()
            content = data.get("content", "")

            if not fpath:
                messages.append({"role": "user", "content": "[에러]: 'file' 경로가 비어있습니다. 다시 출력하세요."})
                continue

            dir_name = os.path.dirname(fpath)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

            print(f"📝 [파일 작성]: '{fpath}'")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

            messages.append({
                "role": "user",
                "content": f"[시스템 알림]: '{fpath}' 저장이 완료되었습니다. 추가 작업이 필요하면 다음 JSON 액션을 실행하고, 끝났다면 'reply'로 최종 보고하세요."
            })

        # 2) shell 액션 (PowerShell 명령 실행 - 한글 깨짐 방지를 위해 UTF-8 출력 인코딩 강제 적용)
        elif action == "shell":
            cmd = data.get("command", "").strip()
            print(f"💻 [명령 실행]: {cmd}")
            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", utf8_cmd],
                    capture_output=True, shell=True, timeout=15
                )
                stdout_str = safe_decode(res.stdout)
                stderr_str = safe_decode(res.stderr)

                if res.returncode != 0 or stderr_str:
                    err = stderr_str or stdout_str
                    print(f"❌ [실행 에러]:\n{err}")
                    messages.append({
                        "role": "user",
                        "content": f"[에러 발생]:\n{err}\n\n원인을 파악해 명령어나 코드를 수정하여 다시 시도하세요."
                    })
                else:
                    out = stdout_str or "(출력 없이 정상 실행됨)"
                    print(f"👀 [실행 결과]: {out}")
                    messages.append({
                        "role": "user",
                        "content": f"[실행 결과]: {out}\n\n결과를 바탕으로 추가 작업이 필요하면 계속 진행하고, 완료되었다면 'reply'로 사용자에게 답변하세요."
                    })
            except subprocess.TimeoutExpired:
                print("🪟 [백그라운드/GUI 실행 감지]: 프로세스가 백그라운드에서 유지되고 있습니다.")
                messages.append({
                    "role": "user",
                    "content": "[시스템 알림]: 프로그램이 백그라운드에서 정상 실행되었습니다. 'reply'로 완료 보고하세요."
                })

        # 3) reply 액션 (일반 대화 답변 및 최종 보고)
        elif action == "reply":
            print("\n💬 [에이전트 답변]:")
            print(data.get("message", ""))
            break

        else:
            print(f"⚠️ [알 수 없는 액션]: {data}")
            messages.append({
                "role": "user",
                "content": "[에러]: 'action' 값은 반드시 'write', 'shell', 'reply' 중 하나여야 합니다."
            })
    else:
        print("\n⚠️ [안내]: 최대 실행 단계(7단계)에 도달하여 작업을 중단했습니다.")
