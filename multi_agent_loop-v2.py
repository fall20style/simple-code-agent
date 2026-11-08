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

# 역할별 max_tokens 안전 상한 설정 (무한 반복 루프 시 대기 시간 최소화)
MAX_TOKENS_ORCHESTRATOR = 768
MAX_TOKENS_SUBAGENT = 2048

MAX_ORCHESTRATOR_STEPS = 8
MAX_SUBAGENT_STEPS = 6
MAX_HISTORY = 24
MAX_TOOL_OUTPUT = 6000

PROJECT_DOC_FILES = ["AGENT.md", "CLAUDE.md", "README.md", "SETUP.md"]
MAX_DOC_CHARS_PER_FILE = 1200
MAX_PROJECT_DOC_CHARS = 3000

MAX_LIST_ENTRIES = 200
MAX_GLOB_RESULTS = 200
MAX_GREP_RESULTS = 120
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_LINE_CHARS = 300
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}

# 런타임 전역 설정 (CLI 인자로 초기화)
AUTO_APPROVE = False
WORKSPACE_ROOT = os.path.abspath(".")

# 2. 기본 작업 도구 스키마 (서브 에이전트용)
BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file at the given path inside workspace.",
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
            "description": "List files and subdirectories of a directory inside workspace. Directories end with '/'.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path (default: '.')"}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files/directories by glob pattern inside workspace, e.g. '**/*.py'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern such as '**/*.py'"},
                    "path": {"type": "string", "description": "Base directory (default: '.')"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression inside workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "File or directory to search"},
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
            "description": "Write or overwrite full content to a file inside workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path inside workspace"},
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
            "description": "Replace one exact occurrence of old_str with new_str in an existing file inside workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path inside workspace"},
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
            "description": "Execute a Windows PowerShell command inside the workspace directory and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Windows PowerShell command"}},
                "required": ["command"]
            }
        }
    }
]

ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a focused subtask to a specialized sub-agent (planner, worker, or reviewer).",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["planner", "worker", "reviewer"],
                        "description": "Sub-agent role: 'planner' (read-only analysis & plan), 'worker' (write/edit code), 'reviewer' (test & verify)"
                    },
                    "task": {
                        "type": "string",
                        "description": "Clear, self-contained instructions and context for the sub-agent"
                    }
                },
                "required": ["role", "task"]
            }
        }
    }
]

ROLE_ALLOWED_TOOLS = {
    "planner": {"list_dir", "glob", "grep", "read_file"},
    "worker": {"list_dir", "glob", "grep", "read_file", "write_file", "edit_file", "run_powershell"},
    "reviewer": {"list_dir", "glob", "grep", "read_file", "run_powershell"},
}

def get_tools_for_role(role: str) -> list:
    allowed = ROLE_ALLOWED_TOOLS.get(role, set())
    return [t for t in BASE_TOOLS if t["function"]["name"] in allowed]

# 3. 안전 필터, 워크스페이스 격리 검증 및 프로젝트 문서 로더
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
]

def check_dangerous_command(command: str):
    for pattern, label in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return label
    return None

def resolve_workspace_path(rel_or_abs_path: str) -> str:
    """
    요청된 경로를 WORKSPACE_ROOT 내부의 정규화된 절대 경로로 변환합니다.
    상위 디렉터리 탈출(../)이나 외부 드라이브/폴더 접근 시 PermissionError를 발생시킵니다.
    """
    raw = (rel_or_abs_path or ".").strip()
    candidate = raw if os.path.isabs(raw) else os.path.join(WORKSPACE_ROOT, raw)
    resolved = os.path.realpath(candidate)
    ws_real = os.path.realpath(WORKSPACE_ROOT)

    try:
        common = os.path.commonpath([ws_real, resolved])
    except ValueError:
        raise PermissionError(f"Access denied: '{rel_or_abs_path}' is on a different drive outside workspace.")

    if common != ws_real:
        raise PermissionError(f"Access denied: '{rel_or_abs_path}' escapes workspace boundary ({ws_real}).")

    return resolved

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

# 4. 로컬 탐색 및 파일/셸 도구 구현 (워크스페이스 격리 적용)
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

    safe_results = []
    for match in matches:
        norm = match.replace("\\", "/")
        if set(norm.split("/")) & SKIP_DIRS:
            continue
        try:
            full_p = resolve_workspace_path(os.path.join(base, match))
            safe_results.append(norm + ("/" if os.path.isdir(full_p) else ""))
        except PermissionError:
            continue

    results = sorted(set(safe_results))
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
            resolve_workspace_path(fpath)
            if os.path.getsize(fpath) > MAX_GREP_FILE_BYTES or _is_binary_file(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{_format_path(rel_base, fpath)}:{lineno}: {line.rstrip()[:MAX_GREP_LINE_CHARS]}")
                        if len(matches) >= MAX_GREP_RESULTS:
                            return "\n".join(matches)
        except (OSError, PermissionError):
            continue
    return "\n".join(matches) if matches else f"No matches for '{pattern}'."

def request_approval(prompt_text: str, caller_tag: str) -> bool:
    if AUTO_APPROVE:
        print(f"  ⚡ [{caller_tag} 자동 승인] {prompt_text}")
        return True
    confirm = input(f"\n⚠️ [{caller_tag} 승인] {prompt_text} (y/n): ")
    return confirm.lower() == "y"

def execute_base_tool(name: str, args: dict, caller_tag: str = "agent") -> str:
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
            if not request_approval(f"'{args['path']}' 파일을 저장할까요?", caller_tag):
                return "User rejected file write."
            dir_name = os.path.dirname(target)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Success: '{args['path']}' saved ({len(args['content'].splitlines())} lines)."
        elif name == "edit_file":
            target = resolve_workspace_path(args["path"])
            old_str, new_str = args["old_str"], args["new_str"]
            if not old_str:
                return "Error: 'old_str' must not be empty."
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            occ = content.count(old_str)
            if occ == 0:
                return f"Error: 'old_str' not found in '{args['path']}'."
            if occ > 1:
                return f"Error: 'old_str' appears {occ} times in '{args['path']}'."
            if not request_approval(f"'{args['path']}' 파일을 부분 수정할까요?", caller_tag):
                return "User rejected file edit."
            with open(target, "w", encoding="utf-8") as f:
                f.write(content.replace(old_str, new_str, 1))
            return f"Success: '{args['path']}' updated."
        elif name == "run_powershell":
            cmd = args["command"].strip()
            danger = check_dangerous_command(cmd)
            if danger:
                print(f"\n🚫 [{caller_tag} 차단] 위험 명령어 감지: {danger}\nPS> {cmd}")
                return f"BLOCKED by safety filter ({danger})."
            if not request_approval(f"PowerShell 실행? -> {cmd}", caller_tag):
                return "User rejected command."

            # Python 스크립트 실행 시 워크스페이스 내부 파일인지 검증 후 비동기 감지 실행
            py_match = re.match(r'^(?:Start-Process\s+)?python(?:\.exe)?\s+["\']?([^\s"\']+\.py)["\']?$', cmd, re.IGNORECASE)
            if py_match:
                target_py = resolve_workspace_path(py_match.group(1))
                proc = subprocess.Popen(
                    [sys.executable, target_py],
                    cwd=WORKSPACE_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace"
                )
                try:
                    out, err = proc.communicate(timeout=2.5)
                    return f"ExitCode: {proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                except subprocess.TimeoutExpired:
                    return f"Success: GUI/long-running script '{py_match.group(1)}' launched inside workspace and running without errors."

            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", utf8_cmd],
                cwd=WORKSPACE_ROOT,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        return f"Unknown tool: {name}"
    except PermissionError as pe:
        print(f"\n🛡️ [격리 차단] {pe}")
        return f"Security Error: {str(pe)}"
    except Exception as e:
        return f"Execution error: {str(e)}"

# 5. 스트리밍 LLM 호출 헬퍼 (역할별 max_tokens 안전 상한 + 실시간 진행 표시)
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

# 7. 서브 에이전트 실행 엔진 (Planner / Worker / Reviewer)
def build_subagent_prompt(role: str, project_docs: str) -> str:
    role_guides = {
        "planner": (
            "You are the PLANNER sub-agent (READ-ONLY). "
            "Explore the workspace using list_dir, glob, grep, and read_file. "
            "Produce a concise, concrete step-by-step implementation plan specifying target files and exact changes. "
            "Prefer Python's built-in `tkinter` for GUI apps unless external libraries are already installed. "
            "Do NOT attempt to modify files."
        ),
        "worker": (
            "You are the WORKER sub-agent. "
            "Implement the requested code changes accurately using read_file, edit_file, write_file, and run_powershell. "
            "Write clean, concise, complete code without unnecessary repetition or bloat. Summarize what you changed when done."
        ),
        "reviewer": (
            "You are the REVIEWER sub-agent. "
            "Verify the recent changes by reading modified files and running tests or launching the script via run_powershell. "
            "Report PASS/FAIL clearly with any errors or edge cases found."
        ),
    }

    example_tool = "list_dir" if role == "planner" else ("edit_file" if role == "worker" else "run_powershell")
    example_args = '{"path": "."}' if role == "planner" else ('{"path": "app.py", "old_str": "x = 1", "new_str": "x = 2"}' if role == "worker" else '{"command": "python app.py"}')

    return f"""/no_think
{role_guides[role]}
Isolated Workspace Directory: {WORKSPACE_ROOT}
All file paths MUST be relative to the workspace root (e.g., 'tetris.py', 'src/main.py'). Do NOT access parent directories ('../').
When calling a tool, respond ONLY with a JSON block:
```json
{{
    "name": "{example_tool}",
    "arguments": {example_args}
}}
```
When your sub-task is complete, output a clear plain-text report (no JSON).
{project_docs}"""

def run_subagent(role: str, task: str, project_docs: str) -> str:
    if role not in ROLE_ALLOWED_TOOLS:
        return f"Error: invalid sub-agent role '{role}'. Choose from planner, worker, reviewer."

    print(f"\n🚀 [SUB-AGENT:{role.upper()}] 작업 시작 → {task[:80]}{'...' if len(task) > 80 else ''}")
    tools = get_tools_for_role(role)
    allowed_names = ROLE_ALLOWED_TOOLS[role]
    messages = [
        {"role": "system", "content": build_subagent_prompt(role, project_docs)},
        {"role": "user", "content": task}
    ]

    for step in range(1, MAX_SUBAGENT_STEPS + 1):
        res = stream_chat_completion(
            messages, tools=tools, temperature=0.15,
            max_tokens=MAX_TOKENS_SUBAGENT, label=role
        )
        content = res["content"]
        raw_tcs = res["tool_calls"]
        calls = []
        using_fallback = False

        if raw_tcs:
            assistant_msg = {"role": "assistant", "tool_calls": raw_tcs}
            if content:
                assistant_msg["content"] = content
            messages.append(assistant_msg)
            calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in raw_tcs]
        else:
            fb = extract_fallback_tool_calls(content)
            if fb:
                calls = fb
                using_fallback = True
                messages.append({"role": "assistant", "content": content})
            elif looks_like_tool_call(content):
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "[JSON parse error] Send one valid JSON tool call. /no_think"})
                continue
            else:
                result_text = content or "(No output)"
                print(f"✅ [SUB-AGENT:{role.upper()}] 완료 (Step {step})")
                return result_text

        for call in calls:
            fname = call["name"]
            if fname not in allowed_names:
                out = f"Permission denied: role '{role}' cannot use tool '{fname}'."
            else:
                try:
                    pargs = json.loads(call["arguments"])
                    preview_args = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v) for k, v in pargs.items()}
                    print(f"  🔧 [{role}] {fname}({preview_args})")
                    out = execute_base_tool(fname, pargs, caller_tag=role.upper())
                except Exception as e:
                    out = f"Tool error: {e}"

            if using_fallback:
                messages.append({"role": "user", "content": f"[Tool result: {fname}]\n{str(out)[:MAX_TOOL_OUTPUT]} /no_think"})
            else:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(out)[:MAX_TOOL_OUTPUT]})

    print(f"⚠️ [SUB-AGENT:{role.upper()}] 최대 스텝({MAX_SUBAGENT_STEPS}) 도달")
    return f"Sub-agent '{role}' reached max steps. Last context: {str(messages[-1].get('content', ''))[:1000]}"

# 8. 메인 오케스트레이터 루프
def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY:
        return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

def build_orchestrator_prompt(project_docs: str) -> str:
    return f"""/no_think
You are the ORCHESTRATOR of a Windows multi-agent coding system.
Isolated Workspace Directory: {WORKSPACE_ROOT}

You do NOT edit files or run commands directly. Instead, delegate subtasks using `delegate_task(role, task)`:
- "planner": Inspects files and creates a step-by-step plan (read-only).
- "worker": Writes or edits code files based on a clear plan.
- "reviewer": Runs tests/commands and verifies the worker's changes.

Typical workflow for coding tasks:
1. Call `delegate_task` with role="planner" to inspect the workspace and design the change.
2. Call `delegate_task` with role="worker" passing the planner's findings and exact instructions.
3. Call `delegate_task` with role="reviewer" to verify the changes work properly.
(For simple read-only questions, calling "planner" alone is enough.)

When all necessary sub-agents have finished, provide a final Korean summary to the user (plain text, no JSON).
{project_docs}"""

def orchestrator_loop():
    project_docs = load_project_docs(WORKSPACE_ROOT)
    messages = [{"role": "system", "content": build_orchestrator_prompt(project_docs)}]

    mode_badge = "⚡ AUTO-APPROVE ON" if AUTO_APPROVE else "🔒 INTERACTIVE APPROVAL"
    print("🤖 [ORCHESTRATOR] 멀티 에이전트 코딩 시스템 v3 시작 (Planner / Worker / Reviewer)")
    print(f"📂 격리 작업 디렉터리: {WORKSPACE_ROOT}")
    print(f"🛡️ 실행 보안 모드: {mode_badge}")
    print(f"⚙️ 토큰 상한 설정: Orchestrator={MAX_TOKENS_ORCHESTRATOR} / Sub-Agent={MAX_TOKENS_SUBAGENT}")
    if project_docs:
        print(f"📚 프로젝트 지침 자동 로드 완료 ({len(project_docs)}자)")

    while True:
        user_input = input("\n👤 사용자: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        messages.append({"role": "user", "content": user_input})
        messages = trim_context(messages)

        for step in range(1, MAX_ORCHESTRATOR_STEPS + 1):
            res = stream_chat_completion(
                messages, tools=ORCHESTRATOR_TOOLS, temperature=0.2,
                max_tokens=MAX_TOKENS_ORCHESTRATOR, label="orchestrator"
            )
            content = res["content"]
            raw_tcs = res["tool_calls"]
            calls = []
            using_fallback = False

            if raw_tcs:
                print(f"\n📡 [ORCHESTRATOR Step {step}] 표준 Tool Calling 위임")
                assistant_msg = {"role": "assistant", "tool_calls": raw_tcs}
                if content:
                    assistant_msg["content"] = content
                messages.append(assistant_msg)
                calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in raw_tcs]
            else:
                fb = extract_fallback_tool_calls(content)
                if fb:
                    print(f"\n⚠️ [ORCHESTRATOR Step {step}] Fallback 파싱 위임")
                    calls = fb
                    using_fallback = True
                    messages.append({"role": "assistant", "content": content})
                elif looks_like_tool_call(content):
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "[JSON parse error] Send valid JSON for delegate_task. /no_think"})
                    continue
                else:
                    print(f"\n💬 [ORCHESTRATOR Step {step}] 최종 응답 완료")
                    messages.append({"role": "assistant", "content": content})
                    if content:
                        print(f"🤖 오케스트레이터: {content}")
                    break

            for call in calls:
                if call["name"] != "delegate_task":
                    out = f"Error: Orchestrator can only call 'delegate_task', not '{call['name']}'."
                else:
                    try:
                        pargs = json.loads(call["arguments"])
                        out = run_subagent(pargs["role"], pargs["task"], project_docs)
                    except Exception as e:
                        out = f"Delegation error: {e}"

                if using_fallback:
                    messages.append({
                        "role": "user",
                        "content": f"[Sub-agent report]\n{str(out)[:MAX_TOOL_OUTPUT]}\n\n"
                                   "Decide whether to delegate to the next sub-agent (e.g. worker or reviewer) or give the final Korean answer. /no_think"
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": str(out)[:MAX_TOOL_OUTPUT]
                    })
        else:
            print(f"\n⚠️ 오케스트레이터 최대 단계({MAX_ORCHESTRATOR_STEPS}회) 도달")

def parse_cli_args():
    global AUTO_APPROVE, WORKSPACE_ROOT
    parser = argparse.ArgumentParser(description="Windows Multi-Agent Coding System v3")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="파일 쓰기/수정 및 셸 실행 시 사용자 승인(y/n)을 생략하고 자동 실행 (위험 명령어 차단은 항상 유지)"
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=".",
        help="에이전트가 접근할 수 있는 격리된 작업 디렉터리 경로 (기본값: 현재 디렉터리)"
    )
    args = parser.parse_args()
    AUTO_APPROVE = args.auto
    WORKSPACE_ROOT = os.path.abspath(args.workspace)
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)

if __name__ == "__main__":
    parse_cli_args()
    orchestrator_loop()
