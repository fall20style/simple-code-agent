import os
import ast
import argparse
import glob as glob_module
import fnmatch
import json
import re
import subprocess
import sys
import time
import uuid
from openai import OpenAI

# Windows 콘솔 UTF-8 고정 및 로컬 프록시 우회
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["no_proxy"] = "localhost,127.0.0.1"

# 1. 로컬 LLM 서버 연결 설정
client = OpenAI(base_url="http://localhost:8000/v1", api_key="local-key")
MODEL_NAME = "Qwen3-4B-Q4_K_M.gguf"

MAX_AGENT_STEPS = 12
MAX_HISTORY = 28
MAX_TOOL_OUTPUT = 6000
MAX_TOKENS_AGENT = 2048

PROJECT_DOC_FILES = ["AGENT.md", "CLAUDE.md", "README.md", "SETUP.md"]
MAX_DOC_CHARS_PER_FILE = 1200
MAX_PROJECT_DOC_CHARS = 3000

MAX_LIST_ENTRIES = 200
MAX_GLOB_RESULTS = 200
MAX_GREP_RESULTS = 120
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_LINE_CHARS = 300
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}

# 전역 런타임 설정 (CLI 플래그로 초기화)
AUTO_APPROVE = False
WORKSPACE_ROOT = os.path.abspath(".")

# 2. 단일 자율 에이전트 통합 도구 스키마
ALL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file at the given path inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative file path inside workspace"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories of a directory inside the workspace. Directories end with '/'.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative directory path (default: '.')"}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files/directories by glob pattern inside the workspace, e.g. '**/*.py'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern such as '**/*.py'"},
                    "path": {"type": "string", "description": "Relative base directory (default: '.')"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Relative file or directory to search"},
                    "include": {"type": "string", "description": "File glob filter, e.g. '*.py'"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite full content to a file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path to save"},
                    "content": {"type": "string", "description": "Full text/code content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_str with new_str in an existing file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path to edit"},
                    "old_str": {"type": "string", "description": "Exact existing text to replace"},
                    "new_str": {"type": "string", "description": "New text to replace with"}
                },
                "required": ["path", "old_str", "new_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell",
            "description": "Execute a Windows PowerShell command inside the isolated workspace directory and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Windows PowerShell command"}},
                "required": ["command"]
            }
        }
    }
]

# 3. 안전 필터 및 경로 격리 검증기
DANGEROUS_PATTERNS = [
    (r"remove-item\b(?=[^\n]*-recurse)(?=[^\n]*-force)", "Remove-Item -Recurse -Force"),
    (r"\b(?:rm|del|erase|rd|rmdir)\b[^\n]*\s-r(?:ec(?:urse)?)?\b[^\n]*\s-fo(?:rce)?\b", "recursive force delete alias"),
    (r"\bformat-volume\b", "Format-Volume"),
    (r"\bclear-disk\b", "Clear-Disk"),
    (r"\binitialize-disk\b", "Initialize-Disk"),
    (r"\bformat\s+[a-z]:", "format <drive>:"),
    (r"\bmkfs(?:\.\w+)?\b", "mkfs"),
    (r"\bdiskpart\b", "diskpart"),
    (r"\bcipher\s+/w", "cipher /w"),
    (r"\bstop-computer\b", "Stop-Computer"),
    (r"\brestart-computer\b", "Restart-Computer"),
    (r"\bshutdown\b", "shutdown"),
    (r"\bset-location\s+[a-z]:\\", "drive root navigation"),
    (r"\bcd\s+/d\s+[a-z]:\\", "drive root navigation"),
]

def check_dangerous_command(command: str):
    for pattern, label in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return label
    return None

def resolve_workspace_path(user_path: str) -> str:
    """요청된 경로가 WORKSPACE_ROOT 내부에 있는지 검증하고 절대 경로를 반환"""
    raw = (user_path or ".").strip()
    candidate = os.path.abspath(os.path.join(WORKSPACE_ROOT, raw))
    try:
        common = os.path.commonpath([WORKSPACE_ROOT, candidate])
    except ValueError:
        raise PermissionError(f"접근 거부: 다른 드라이브 경로('{user_path}')는 허용되지 않습니다.")
    if common != WORKSPACE_ROOT:
        raise PermissionError(f"접근 거부: 작업 디렉터리('{WORKSPACE_ROOT}') 외부 경로('{user_path}')는 차단됩니다.")
    return candidate

def request_approval(prompt_msg: str) -> bool:
    """--auto 모드 여부에 따라 자동 승인 또는 사용자 수동 승인 요청"""
    if AUTO_APPROVE:
        print(f"⚡ [AUTO-APPROVED] {prompt_msg}")
        return True
    confirm = input(f"\n⚠️ [수동 승인 필요] {prompt_msg} (y/n): ")
    return confirm.strip().lower() == "y"

def load_project_docs(base_dir: str) -> str:
    sections = []
    remaining = MAX_PROJECT_DOC_CHARS
    for filename in PROJECT_DOC_FILES:
        if remaining <= 0:
            break
        fpath = os.path.join(base_dir, filename)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
        except OSError:
            continue
        if not text:
            continue
        limit = min(remaining, MAX_DOC_CHARS_PER_FILE)
        snippet = text[:limit] + ("\n...(truncated)" if len(text) > limit else "")
        sections.append(f"--- {filename} ---\n{snippet}")
        remaining -= limit
    return ("Project instructions:\n" + "\n\n".join(sections)) if sections else ""

# 4. 로컬 탐색 및 파일/셸 도구 구현 (WORKSPACE_ROOT 격리 적용)
def _format_path(base_dir: str, path: str) -> str:
    rel = os.path.relpath(path, base_dir)
    return "." if rel == "." else rel.replace(os.sep, "/")

def _is_binary_file(fpath: str) -> bool:
    try:
        with open(fpath, "rb") as f:
            return b"\x00" in f.read(1024)
    except OSError:
        return True

def list_dir_tool(path: str = ".") -> str:
    target = resolve_workspace_path(path or ".")
    if not os.path.isdir(target):
        return f"Error: '{path}' is not a directory."
    try:
        names = os.listdir(target)
    except OSError as e:
        return f"Error: {e}"
    if not names:
        return f"Directory '{path}' is empty."
    names.sort(key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))
    lines = [name + "/" if os.path.isdir(os.path.join(target, name)) else name for name in names[:MAX_LIST_ENTRIES]]
    return f"Directory: {path}\n" + "\n".join(lines)

def glob_tool(pattern: str, path: str = ".") -> str:
    base = resolve_workspace_path(path or ".")
    if not os.path.isdir(base):
        return f"Error: '{path}' is not a directory."
    try:
        matches = glob_module.glob(pattern, root_dir=base, recursive=True)
    except (ValueError, re.error) as e:
        return f"Error: invalid glob '{pattern}': {e}"
    results = sorted({
        match.replace("\\", "/") + ("/" if os.path.isdir(os.path.join(base, match)) else "")
        for match in matches if not (set(match.replace("\\", "/").split("/")) & SKIP_DIRS)
    })
    return f"Glob '{pattern}':\n" + "\n".join(results[:MAX_GLOB_RESULTS]) if results else f"No matches for '{pattern}'."

def grep_tool(pattern: str, path: str = ".", include: str = "*") -> str:
    base = resolve_workspace_path(path or ".")
    if not os.path.exists(base):
        return f"Error: '{path}' does not exist."
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex '{pattern}': {e}"
    include = include or "*"
    rel_base = base if os.path.isdir(base) else os.path.dirname(base)
    matches = []

    def iter_files():
        if os.path.isfile(base):
            yield base
            return
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fn in sorted(files):
                if fnmatch.fnmatch(fn, include):
                    yield os.path.join(root, fn)

    for fpath in iter_files():
        try:
            if os.path.getsize(fpath) > MAX_GREP_FILE_BYTES or _is_binary_file(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{_format_path(rel_base, fpath)}:{lineno}: {line.rstrip()[:MAX_GREP_LINE_CHARS]}")
                        if len(matches) >= MAX_GREP_RESULTS:
                            return "\n".join(matches)
        except OSError:
            continue
    return "\n".join(matches) if matches else f"No matches for '{pattern}'."

def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "list_dir":
            return list_dir_tool(args.get("path", "."))
        elif name == "glob":
            return glob_tool(args["pattern"], args.get("path", "."))
        elif name == "grep":
            return grep_tool(args["pattern"], args.get("path", "."), args.get("include", "*"))
        elif name == "read_file":
            target = resolve_workspace_path(args["path"])
            with open(target, "r", encoding="utf-8") as f:
                return f.read()
        elif name == "write_file":
            target = resolve_workspace_path(args["path"])
            rel_display = _format_path(WORKSPACE_ROOT, target)
            if not request_approval(f"[AGENT] '{rel_display}' 파일 저장"):
                return "User rejected file write."
            dir_name = os.path.dirname(target)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Success: '{rel_display}' saved ({len(args['content'].splitlines())} lines)."
        elif name == "edit_file":
            target = resolve_workspace_path(args["path"])
            rel_display = _format_path(WORKSPACE_ROOT, target)
            old_str, new_str = args["old_str"], args["new_str"]
            if not old_str:
                return "Error: 'old_str' must not be empty."
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            occ = content.count(old_str)
            if occ == 0:
                return f"Error: 'old_str' not found in '{rel_display}'."
            if occ > 1:
                return f"Error: 'old_str' appears {occ} times in '{rel_display}'."
            if not request_approval(f"[AGENT] '{rel_display}' 파일 부분 수정"):
                return "User rejected file edit."
            with open(target, "w", encoding="utf-8") as f:
                f.write(content.replace(old_str, new_str, 1))
            return f"Success: '{rel_display}' updated."
        elif name == "run_powershell":
            cmd = args["command"].strip()
            danger = check_dangerous_command(cmd)
            if danger:
                print(f"\n🚫 [AGENT 차단] 위험 명령어 감지: {danger}\nPS> {cmd}")
                return f"BLOCKED by safety filter ({danger})."
            if not request_approval(f"[AGENT] PowerShell 실행 -> {cmd}"):
                return "User rejected command."

            # Python 스크립트 실행 시 워크스페이스 내부 경로 검증 및 GUI 앱 비동기 감지
            py_match = re.match(r'^(?:Start-Process\s+)?python(?:\.exe)?\s+["\']?([^\s"\']+\.py)["\']?$', cmd, re.IGNORECASE)
            if py_match:
                target_py_abs = resolve_workspace_path(py_match.group(1))
                rel_py = _format_path(WORKSPACE_ROOT, target_py_abs)
                proc = subprocess.Popen(
                    [sys.executable, target_py_abs],
                    cwd=WORKSPACE_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace"
                )
                try:
                    out, err = proc.communicate(timeout=2.5)
                    return f"ExitCode: {proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                except subprocess.TimeoutExpired:
                    return f"Success: GUI/long-running script '{rel_py}' launched in workspace and running without errors."

            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", utf8_cmd],
                cwd=WORKSPACE_ROOT,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        return f"Unknown tool: {name}"
    except PermissionError as pe:
        print(f"\n🛡️ [경로 격리 차단] {pe}")
        return f"SECURITY BLOCKED: {pe}"
    except Exception as e:
        return f"Execution error: {str(e)}"

# 5. 스트리밍 LLM 호출 헬퍼 (실시간 진행 상황 + 반복 루프 조기 차단 + /no_think 적용)
def stream_chat_completion(messages: list, tools: list, temperature: float, max_tokens: int, label: str) -> dict:
    start_time = time.time()
    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}}
    )

    content_chunks = []
    tool_calls_map = {}
    total_chars = 0

    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        if delta.content:
            content_chunks.append(delta.content)
            total_chars += len(delta.content)

            # 동일 특수문자/토큰 무한 반복(예: //////) 조기 감지 및 차단
            if len(content_chunks) > 25:
                tail = "".join(content_chunks[-25:])
                if len(set(tail)) <= 2 and len(tail) >= 25:
                    sys.stdout.write("\r" + " " * 60 + "\r")
                    print(f"⚠️ [{label}] 반복 출력 루프가 감지되어 조기 중단했습니다.")
                    break

        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": tc.id or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": "", "arguments": ""}
                    }
                if tc.id:
                    tool_calls_map[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        tool_calls_map[idx]["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_map[idx]["function"]["arguments"] += tc.function.arguments
                        total_chars += len(tc.function.arguments)

        elapsed = time.time() - start_time
        sys.stdout.write(f"\r  ⏳ [{label} 생성 중...] {total_chars}자 ({elapsed:.1f}초)   ")
        sys.stdout.flush()

    if total_chars > 0:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    full_content = "".join(content_chunks)
    full_content = re.sub(r'<think>.*?</think>', '', full_content, flags=re.DOTALL).strip()

    tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] if tool_calls_map else []
    return {"content": full_content, "tool_calls": tool_calls}

# 6. JSON 복구 및 폴백 파서
def looks_like_tool_call(content: str) -> bool:
    return bool(content and re.search(r'"name"\s*:', content) and re.search(r'"arguments"\s*:', content))

def _repair_pseudo_json(text: str) -> str:
    def repl(m):
        return f"{json.dumps(m.group(1))}: {json.dumps(m.group(2), ensure_ascii=False)}"
    for pat in [r'"([A-Za-z_]\w*)"\s*:\s*"""(.*?)"""', r'"([A-Za-z_]\w*)"\s*:\s*```(.*?)```', r'"([A-Za-z_]\w*)"\s*:\s*`(.*?)`']:
        text = re.sub(pat, repl, text, flags=re.DOTALL)
    return text

def extract_fallback_tool_calls(content: str) -> list:
    if not content:
        return []
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = match.group(1) if match else content.strip()
    data = None
    for cand in [json_str, _repair_pseudo_json(json_str)]:
        try:
            data = json.loads(cand)
            break
        except Exception:
            try:
                data = ast.literal_eval(cand)
                break
            except Exception:
                pass
    if isinstance(data, dict) and "name" in data and "arguments" in data:
        return [{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "name": data["name"],
            "arguments": data["arguments"] if isinstance(data["arguments"], str) else json.dumps(data["arguments"], ensure_ascii=False)
        }]
    return []

# 7. 단일 자율 에이전트 통합 프롬프트 및 실행 루프
def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY:
        return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

def build_single_agent_prompt(workspace_dir: str, project_docs: str) -> str:
    return f"""/no_think
You are an autonomous Windows Single Coding Agent.
Isolated working directory: {workspace_dir}
All file paths must be relative to this workspace directory (do NOT use '..' or paths outside the workspace).

You handle the ENTIRE workflow yourself from start to finish:
1. Explore: Use `list_dir`, `glob`, `grep`, or `read_file` to inspect existing files if needed.
2. Implement: Use `write_file` or `edit_file` to write clean, complete, working code. Prefer Python's built-in `tkinter` for GUI apps unless external packages are requested.
3. Verify: Use `run_powershell` (e.g. `python <filename>.py`) to execute and test your code. Fix any errors immediately if they occur.
4. Report: Once verified, provide a clear, concise final summary in Korean (plain text, no JSON).

When calling a tool, respond ONLY with a valid tool call (or JSON block):
```json
{{
    "name": "write_file",
    "arguments": {{"path": "tetris.py", "content": "..."}}
}}
```
{project_docs}"""

def single_agent_loop():
    project_docs = load_project_docs(WORKSPACE_ROOT)
    messages = [{"role": "system", "content": build_single_agent_prompt(WORKSPACE_ROOT, project_docs)}]

    mode_badge = "⚡ AUTO-APPROVE (무인 자동 승인)" if AUTO_APPROVE else "🔒 INTERACTIVE APPROVAL (수동 승인)"
    print("🤖 [SINGLE AGENT] 단일 자율 코딩 에이전트 시작 (탐색 → 작성 → 실행 → 검증 통합)")
    print(f"📂 격리 작업 디렉터리: {WORKSPACE_ROOT}")
    print(f"🛡️ 실행 보안 모드: {mode_badge}")
    print(f"⚙️ 최대 스텝 / 토큰 상한: {MAX_AGENT_STEPS} steps / {MAX_TOKENS_AGENT} tokens")
    if project_docs:
        print(f"📚 프로젝트 지침 자동 로드 완료 ({len(project_docs)}자)")

    while True:
        user_input = input("\n👤 사용자: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "종료"):
            break

        messages.append({"role": "user", "content": user_input})
        messages = trim_context(messages)

        for step in range(1, MAX_AGENT_STEPS + 1):
            res = stream_chat_completion(
                messages,
                tools=ALL_TOOLS,
                temperature=0.15,
                max_tokens=MAX_TOKENS_AGENT,
                label=f"agent Step {step}"
            )
            content = res["content"]
            raw_tcs = res["tool_calls"]
            calls = []
            using_fallback = False

            if raw_tcs:
                print(f"\n🔧 [Step {step}/{MAX_AGENT_STEPS}] 표준 Tool Calling 실행")
                assistant_msg = {"role": "assistant", "tool_calls": raw_tcs}
                if content:
                    assistant_msg["content"] = content
                messages.append(assistant_msg)
                calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in raw_tcs]
            else:
                fb = extract_fallback_tool_calls(content)
                if fb:
                    print(f"\n⚠️ [Step {step}/{MAX_AGENT_STEPS}] Fallback 파싱 실행")
                    calls = fb
                    using_fallback = True
                    messages.append({"role": "assistant", "content": content})
                elif looks_like_tool_call(content):
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "[JSON parse error] Send one valid JSON tool call. /no_think"})
                    continue
                else:
                    print(f"\n💬 [Step {step}/{MAX_AGENT_STEPS}] 최종 응답 완료")
                    messages.append({"role": "assistant", "content": content})
                    if content:
                        print(f"🤖 에이전트: {content}")
                    break

            for call in calls:
                fname = call["name"]
                try:
                    pargs = json.loads(call["arguments"])
                    preview_args = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v) for k, v in pargs.items()}
                    print(f"  ▶ {fname}({preview_args})")
                    out = execute_tool(fname, pargs)
                except Exception as e:
                    out = f"Tool error: {e}"

                if using_fallback:
                    messages.append({
                        "role": "user",
                        "content": f"[Tool result: {fname}]\n{str(out)[:MAX_TOOL_OUTPUT]}\n\n"
                                   "Continue with the next tool call if needed, or provide the final Korean summary. /no_think"
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": str(out)[:MAX_TOOL_OUTPUT]
                    })
        else:
            print(f"\n⚠️ 에이전트 최대 단계({MAX_AGENT_STEPS}회) 도달")

def parse_args():
    parser = argparse.ArgumentParser(description="단일 자율 로컬 코딩 에이전트 (Single Coding Agent)")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="파일 쓰기/수정 및 PowerShell 실행 시 사용자 확인(y/n) 없이 자동 승인합니다."
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=".",
        help="에이전트가 접근할 수 있는 격리 작업 디렉터리 경로 (기본값: 현재 디렉터리)"
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    AUTO_APPROVE = args.auto
    WORKSPACE_ROOT = os.path.abspath(args.workspace)
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    os.chdir(WORKSPACE_ROOT)
    single_agent_loop()
