import os
import json
import re
import subprocess
import uuid
from openai import OpenAI

# 1. 실행 중인 llama-server.exe (127.0.0.1:8000) 연결 설정
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="local-key"
)

MODEL_NAME = "qwen2.5-coder-3b-instruct"

MAX_STEPS = 8
MAX_HISTORY = 24
MAX_TOOL_OUTPUT = 6000

# 2. Windows PowerShell 기반 도구(Tools) JSON 스키마
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite full content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to save"},
                    "content": {"type": "string", "description": "Full text/code content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell",
            "description": "Execute a Windows PowerShell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Windows PowerShell command to execute"}
                },
                "required": ["command"]
            }
        }
    }
]

# 3. 로컬 도구 실행기 (PowerShell UTF-8 인코딩 및 사용자 승인 포함)
def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "read_file":
            with open(args["path"], "r", encoding="utf-8") as f:
                return f.read()

        elif name == "write_file":
            confirm = input(f"\n⚠️ [승인] '{args['path']}' 파일을 저장할까요? (y/n): ")
            if confirm.lower() != "y":
                return "User rejected file write."
            with open(args["path"], "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Success: '{args['path']}' saved."

        elif name == "run_powershell":
            cmd = args["command"]
            confirm = input(f"\n⚠️ [승인] PowerShell 명령어를 실행할까요?\nPS> {cmd}\n실행 여부 (y/n): ")
            if confirm.lower() != "y":
                return "User rejected PowerShell command execution."

            # 한글 깨짐 방지를 위해 PowerShell 출력 인코딩을 UTF-8로 강제 설정
            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"

            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", utf8_cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Execution error: {str(e)}"

# 4. 컨텍스트 관리 함수
def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY:
        return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

# 5. 텍스트 JSON 출력 폴백 파서
def extract_fallback_tool_calls(content: str) -> list:
    """llama-server가 tool_calls 대신 일반 텍스트(```json)로 뱉었을 때 추출하는 보조 함수"""
    if not content:
        return []

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = match.group(1) if match else content.strip()

    try:
        data = json.loads(json_str)
        if isinstance(data, dict) and "name" in data and "arguments" in data:
            return [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": data["name"],
                "arguments": data["arguments"] if isinstance(data["arguments"], str) else json.dumps(data["arguments"], ensure_ascii=False)
            }]
    except Exception:
        pass
    return []

# 6. 에이전트 루프
def agent_loop():
    current_dir = os.getcwd()  # 실제 현재 작업 경로 획득
    
    system_prompt = f"""You are an autonomous Windows CLI coding agent.
Your current working directory is: {current_dir}
You MUST use the provided tools to inspect files and run PowerShell commands.
NEVER guess directory paths or file contents. NEVER tell the user to run a command themselves.
When you need information from the system, you MUST respond ONLY with a JSON tool call in this exact format:

```json
{{
    "name": "run_powershell",
    "arguments": {{
        "command": "Get-ChildItem -File | Measure-Object | Select-Object -ExpandProperty Count"
    }}
}}
```

Available tools:
1. read_file(path): Read a file.
2. write_file(path, content): Write or overwrite a file.
3. run_powershell(command): Run a Windows PowerShell command in {current_dir}.

Always reply to the user in Korean AFTER you receive the tool execution result."""

    messages = [{"role": "system", "content": system_prompt}]

    print("🤖 llama-server(127.0.0.1:8000) Windows 코딩 에이전트 연결 완료 (종료: exit)")

    while True:
        user_input = input("\n👤 사용자: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        messages.append({"role": "user", "content": user_input})
        messages = trim_context(messages)

        step = 0
        while step < MAX_STEPS:
            step += 1

            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=TOOLS,
                temperature=0.2
            )

            assistant_msg = response.choices[0].message

            # 1) 표준 tool_calls 확인
            calls_to_run = []
            if assistant_msg.tool_calls:
                print(f"\n📡 [LOG] Step {step}: LLM 표준 Tool Calling 동작 감지 (tool_calls 필드)")
                messages.append(assistant_msg.model_dump(exclude_unset=True))
                for tc in assistant_msg.tool_calls:
                    calls_to_run.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    })
            else:
                # 2) 텍스트(content)로 JSON을 출력한 경우 폴백 파싱 시도
                fallback_calls = extract_fallback_tool_calls(assistant_msg.content)
                if fallback_calls:
                    print(f"\n⚠️ [LOG] Step {step}: Fallback 파싱 동작 감지 (일반 텍스트에서 JSON 추출)")
                    calls_to_run = fallback_calls
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]}
                        } for c in fallback_calls]
                    })
                else:
                    # 일반 텍스트 최종 답변인 경우 출력 후 내부 루프 종료
                    print(f"\n💬 [LOG] Step {step}: 일반 텍스트 최종 응답 (도구 호출 없음)")
                    messages.append({"role": "assistant", "content": assistant_msg.content})
                    if assistant_msg.content:
                        print(f"🤖 에이전트: {assistant_msg.content}")
                    break

            # 3) 추출된 도구 실행
            for call in calls_to_run:
                func_name = call["name"]
                raw_args = call["arguments"]

                try:
                    parsed_args = json.loads(raw_args)
                    print(f"🔧 도구 호출: {func_name}({parsed_args})")
                    tool_output = execute_tool(func_name, parsed_args)
                except json.JSONDecodeError:
                    tool_output = f"JSON parse error: '{raw_args}' is not valid JSON. Fix the format and retry."
                except Exception as e:
                    tool_output = f"Tool execution error: {str(e)}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": str(tool_output)[:MAX_TOOL_OUTPUT]
                })
        else:
            print(f"\n⚠️ 최대 작업 단계({MAX_STEPS}회)에 도달하여 루프를 중단했습니다.")

if __name__ == "__main__":
    agent_loop()
