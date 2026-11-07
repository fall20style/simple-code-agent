import os
import re
import subprocess
from openai import OpenAI

os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["no_proxy"] = "localhost,127.0.0.1"

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="local-key"
)

def safe_decode(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return b.decode("cp949", errors="replace").strip()

# JSON 대신 XML 태그를 사용해 따옴표/줄바꿈 파싱 오류를 100% 차단한 프롬프트
system_prompt = """당신은 사용자의 요청대로 파일을 작성하고 실행하는 자율 코딩 에이전트입니다.

[도구 사용법 - 반드시 아래 태그 형식을 그대로 사용하세요]
1. 파일 생성/수정:
<write file="파일이름.py">
여기에 파이썬 전체 코드를 작성하세요
</write>

2. PowerShell 명령 실행:
<shell>
실행할 명령어
</shell>

[수칙]
- 코드를 설명만 하지 말고 반드시 <write> 태그로 파일을 먼저 생성하세요.
- GUI 프로그램(tkinter 등)을 실행할 때는 창이 열려도 멈추지 않도록 'Start-Process python 파일이름.py' 명령을 사용하세요.
- 생성과 실행이 모두 끝나면 태그 없이 한국어로 완료 보고만 하세요."""

print("🤖 안정화 코딩 에이전트가 시작되었습니다. (종료: exit)")
print("=" * 65)

while True:
    try:
        user_prompt = input("\n🗣️ [명령 입력]: ").strip()
    except (KeyboardInterrupt, EOFError):
        break

    if not user_prompt:
        continue
    if user_prompt.lower() in ["exit", "quit", "종료"]:
        break

    # [핵심] 새 명령마다 대화 기록을 초기화하여 이전 에러/헛소리 오염 차단
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    print(f"🎯 [작업 시작]: {user_prompt}\n" + "-" * 50)

    max_steps = 5
    for step in range(1, max_steps + 1):
        print(f"\n🔄 [Step {step}/{max_steps}] 추론 중...")

        response = client.chat.completions.create(
            model="local-model",
            messages=messages,
            temperature=0.1,
            max_tokens=800
        )

        answer = response.choices[0].message.content.strip()
        messages.append({"role": "assistant", "content": answer})

        # 1) <write file="..."> 태그 감지
        write_match = re.search(r'<write\s+file=["\'](.*?)["\']>(.*?)</write>', answer, re.DOTALL)
        # 2) <shell> 태그 감지
        shell_match = re.search(r'<shell>(.*?)</shell>', answer, re.DOTALL)

        if write_match:
            fpath = write_match.group(1).strip()
            code = write_match.group(2).strip()
            # 혹시 코드 블록(```python)이 섞여 들어갔다면 제거
            code = re.sub(r'^```python\s*|\s*```$', '', code, flags=re.MULTILINE).strip()

            print(f"📝 [파일 작성]: '{fpath}' 생성 중...")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)

            messages.append({
                "role": "user",
                "content": f"[시스템 알림]: '{fpath}' 파일 작성이 완료되었습니다. 실행이 필요하다면 <shell> 태그로 실행하고, 끝났다면 최종 보고하세요."
            })

        elif shell_match:
            cmd = shell_match.group(1).strip()
            print(f"💻 [명령 실행]: {cmd}")
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    capture_output=True, shell=True, timeout=10
                )
                stdout_str = safe_decode(res.stdout)
                stderr_str = safe_decode(res.stderr)

                if res.returncode != 0 or stderr_str:
                    err = stderr_str or stdout_str
                    print(f"❌ [실행 에러]:\n{err}")
                    messages.append({
                        "role": "user",
                        "content": f"[에러 발생]:\n{err}\n\n<write> 태그로 코드를 수정한 뒤 다시 실행하세요."
                    })
                else:
                    out = stdout_str or "(실행 완료)"
                    print(f"👀 [실행 결과]: {out}")
                    messages.append({
                        "role": "user",
                        "content": f"[실행 결과]: {out}\n\n모든 작업이 완료되었다면 태그 없이 최종 보고를 해주세요."
                    })
            except subprocess.TimeoutExpired:
                print("🪟 [GUI 실행 감지]: 창이 열려 백그라운드에서 유지되고 있습니다.")
                messages.append({
                    "role": "user",
                    "content": "[시스템 알림]: GUI 프로그램 창이 정상적으로 실행되었습니다. 최종 완료 보고를 하세요."
                })
        else:
            print("\n✅ [최종 에이전트 보고]:")
            print(answer)
            break
