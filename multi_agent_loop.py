import os
import ast
import glob as glob_module
import fnmatch
import json
import re
import subprocess
import sys
import uuid
from openai import OpenAI

# Windows 콘솔 UTF-8 고정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 1. 로컬 LLM 서버 연결 설정
client = OpenAI(base_url="http://localhost:8000/v1", api_key="local-key")
MODEL_NAME = "qwen2.5-coder-3b-instruct"

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

# 2. 기본 작업 도구 스키마 (서브 에이전트용)
BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories of a directory. Directories end with '/'.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path (default: current directory)"}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files/directories by glob pattern, e.g. '**/*.py'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern such as '**/*.py'"},
                    "path": {"type": "string", "description": "Base directory (default: current directory)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression.",
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
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_str with new_str in an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
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
            "description": "Execute a Windows PowerShell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Windows PowerShell command"}},
                "required": ["command"]
            }
        }
    }
]

# 오케스트레이터 전용 위임 도구 스키마
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

# 역할별 허용 도구 매핑 (권한 분리)
ROLE_ALLOWED_TOOLS = {
    "planner": {"list_dir", "glob", "grep", "read_file"},
    "worker": {"list_dir", "glob", "grep", "read_file", "write_file", "edit_file", "run_powershell"},
    "reviewer": {"list_dir", "glob", "grep", "read_file", "run_powershell"},
}

def get_tools_for_role(role: str) -> list:
    allowed = ROLE_ALLOWED_TOOLS.get(role, set())
    return [t for t in BASE_TOOLS if t["function"]["name"] in allowed]

# 3. 안전 필터 및 프로젝트 문서 로더
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

# 4. 로컬 탐색 및 파일/셸 도구 구현
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
    target = os.path.abspath(path or ".")
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
    base = os.path.abspath(path or ".")
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
    base = os.path.abspath(path or ".")
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

def execute_base_tool(name: str, args: dict, caller_tag: str = "agent") -> str:
    try:
        if name == "list_dir":
            return list_dir_tool(args.get("path", "."))
        elif name == "glob":
            return glob_tool(args["pattern"], args.get("path", "."))
        elif name == "grep":
            return grep_tool(args["pattern"], args.get("path", "."), args.get("include", "*"))
        elif name == "read_file":
            with open(args["path"], "r", encoding="utf-8") as f:
                return f.read()
        elif name == "write_file":
            confirm = input(f"\n⚠️ [{caller_tag} 승인] '{args['path']}' 파일을 저장할까요? (y/n): ")
            if confirm.lower() != "y":
                return "User rejected file write."
            with open(args["path"], "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Success: '{args['path']}' saved."
        elif name == "edit_file":
            path, old_str, new_str = args["path"], args["old_str"], args["new_str"]
            if not old_str:
                return "Error: 'old_str' must not be empty."
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            occ = content.count(old_str)
            if occ == 0:
                return f"Error: 'old_str' not found in '{path}'."
            if occ > 1:
                return f"Error: 'old_str' appears {occ} times in '{path}'."
            confirm = input(f"\n⚠️ [{caller_tag} 승인] '{path}' 파일을 부분 수정할까요? (y/n): ")
            if confirm.lower() != "y":
                return "User rejected file edit."
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(old_str, new_str, 1))
            return f"Success: '{path}' updated."
        elif name == "run_powershell":
            cmd = args["command"]
            danger = check_dangerous_command(cmd)
            if danger:
                print(f"\n🚫 [{caller_tag} 차단] 위험 명령어 감지: {danger}\nPS> {cmd}")
                return f"BLOCKED by safety filter ({danger})."
            confirm = input(f"\n⚠️ [{caller_tag} 승인] PowerShell 실행?\nPS> {cmd}\n실행 여부 (y/n): ")
            if confirm.lower() != "y":
                return "User rejected command."
            utf8_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd}"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", utf8_cmd],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Execution error: {str(e)}"

# 5. JSON 복구 및 폴백 파서
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

# 6. 서브 에이전트 실행 엔진 (Planner / Worker / Reviewer)
def build_subagent_prompt(role: str, current_dir: str, project_docs: str) -> str:
    role_guides = {
        "planner": (
            "You are the PLANNER sub-agent (READ-ONLY). "
            "Explore the codebase using list_dir, glob, grep, and read_file. "
            "Produce a concise, concrete step-by-step implementation plan specifying target files and exact changes. "
            "Do NOT attempt to modify files."
        ),
        "worker": (
            "You are the WORKER sub-agent. "
            "Implement the requested code changes accurately using read_file, edit_file, write_file, and run_powershell. "
            "Always read a file before editing it. Summarize what you changed when done."
        ),
        "reviewer": (
            "You are the REVIEWER sub-agent. "
            "Verify the recent changes by reading modified files and running tests or linters via run_powershell. "
            "Report PASS/FAIL clearly with any errors or edge cases found."
        ),
    }

    example_tool = "list_dir" if role == "planner" else ("edit_file" if role == "worker" else "run_powershell")
    example_args = '{"path": "."}' if role == "planner" else ('{"path": "app.py", "old_str": "x = 1", "new_str": "x = 2"}' if role == "worker" else '{"command": "pytest"}')

    return f"""{role_guides[role]}
Working directory: {current_dir}
When calling a tool, respond ONLY with a JSON block:
```json
{{
    "name": "{example_tool}",
    "arguments": {example_args}
}}
```
When your sub-task is complete, output a clear plain-text report (no JSON).
{project_docs}"""

def run_subagent(role: str, task: str, current_dir: str, project_docs: str) -> str:
    if role not in ROLE_ALLOWED_TOOLS:
        return f"Error: invalid sub-agent role '{role}'. Choose from planner, worker, reviewer."

    print(f"\n🚀 [SUB-AGENT:{role.upper()}] 작업 시작 → {task[:80]}{'...' if len(task) > 80 else ''}")
    tools = get_tools_for_role(role)
    allowed_names = ROLE_ALLOWED_TOOLS[role]
    messages = [
        {"role": "system", "content": build_subagent_prompt(role, current_dir, project_docs)},
        {"role": "user", "content": task}
    ]

    for step in range(1, MAX_SUBAGENT_STEPS + 1):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=tools,
            temperature=0.15
        )
        msg = response.choices[0].message
        calls = []
        using_fallback = False

        if msg.tool_calls:
            messages.append(msg.model_dump(exclude_unset=True))
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments} for tc in msg.tool_calls]
        else:
            fb = extract_fallback_tool_calls(msg.content)
            if fb:
                calls = fb
                using_fallback = True
                messages.append({"role": "assistant", "content": msg.content})
            elif looks_like_tool_call(msg.content):
                messages.append({"role": "assistant", "content": msg.content})
                messages.append({"role": "user", "content": "[JSON parse error] Send one valid JSON tool call."})
                continue
            else:
                result_text = msg.content or "(No output)"
                print(f"✅ [SUB-AGENT:{role.upper()}] 완료 (Step {step})")
                return result_text

        for call in calls:
            fname = call["name"]
            if fname not in allowed_names:
                out = f"Permission denied: role '{role}' cannot use tool '{fname}'."
            else:
                try:
                    pargs = json.loads(call["arguments"])
                    print(f"  🔧 [{role}] {fname}({pargs})")
                    out = execute_base_tool(fname, pargs, caller_tag=role.upper())
                except Exception as e:
                    out = f"Tool error: {e}"

            if using_fallback:
                messages.append({"role": "user", "content": f"[Tool result: {fname}]\n{str(out)[:MAX_TOOL_OUTPUT]}"})
            else:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(out)[:MAX_TOOL_OUTPUT]})

    print(f"⚠️ [SUB-AGENT:{role.upper()}] 최대 스텝({MAX_SUBAGENT_STEPS}) 도달")
    return f"Sub-agent '{role}' reached max steps. Last context: {messages[-1].get('content', '')[:1000]}"

# 7. 메인 오케스트레이터 루프
def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY:
        return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

def build_orchestrator_prompt(current_dir: str, project_docs: str) -> str:
    return f"""You are the ORCHESTRATOR of a Windows multi-agent coding system.
Working directory: {current_dir}

You do NOT edit files or run commands directly. Instead, delegate subtasks using `delegate_task(role, task)`:
- "planner": Inspects files and creates a step-by-step plan (read-only).
- "worker": Writes or edits code files based on a clear plan.
- "reviewer": Runs tests/commands and verifies the worker's changes.

Typical workflow for coding tasks:
1. Call `delegate_task` with role="planner" to inspect the codebase and design the change.
2. Call `delegate_task` with role="worker" passing the planner's findings and exact instructions.
3. Call `delegate_task` with role="reviewer" to verify the changes work properly.
(For simple read-only questions, calling "planner" alone is enough.)

To delegate, respond ONLY with a JSON block:
```json
{{
    "name": "delegate_task",
    "arguments": {{
        "role": "planner",
        "task": "List files in the current directory and summarize the project structure."
    }}
}}
```

When all necessary sub-agents have finished, provide a final Korean summary to the user (plain text, no JSON).
{project_docs}"""

def orchestrator_loop():
    current_dir = os.getcwd()
    project_docs = load_project_docs(current_dir)
    messages = [{"role": "system", "content": build_orchestrator_prompt(current_dir, project_docs)}]

    print("🤖 [ORCHESTRATOR] 멀티 에이전트 코딩 시스템 시작 (Planner / Worker / Reviewer)")
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
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=ORCHESTRATOR_TOOLS,
                temperature=0.2
            )
            msg = response.choices[0].message
            calls = []
            using_fallback = False

            if msg.tool_calls:
                print(f"\n📡 [ORCHESTRATOR Step {step}] 표준 Tool Calling 위임")
                messages.append(msg.model_dump(exclude_unset=True))
                calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments} for tc in msg.tool_calls]
            else:
                fb = extract_fallback_tool_calls(msg.content)
                if fb:
                    print(f"\n⚠️ [ORCHESTRATOR Step {step}] Fallback 파싱 위임")
                    calls = fb
                    using_fallback = True
                    messages.append({"role": "assistant", "content": msg.content})
                elif looks_like_tool_call(msg.content):
                    messages.append({"role": "assistant", "content": msg.content})
                    messages.append({"role": "user", "content": "[JSON parse error] Send valid JSON for delegate_task."})
                    continue
                else:
                    print(f"\n💬 [ORCHESTRATOR Step {step}] 최종 응답 완료")
                    messages.append({"role": "assistant", "content": msg.content})
                    if msg.content:
                        print(f"🤖 오케스트레이터: {msg.content}")
                    break

            for call in calls:
                if call["name"] != "delegate_task":
                    out = f"Error: Orchestrator can only call 'delegate_task', not '{call['name']}'."
                else:
                    try:
                        pargs = json.loads(call["arguments"])
                        out = run_subagent(pargs["role"], pargs["task"], current_dir, project_docs)
                    except Exception as e:
                        out = f"Delegation error: {e}"

                if using_fallback:
                    messages.append({
                        "role": "user",
                        "content": f"[Sub-agent report]\n{str(out)[:MAX_TOOL_OUTPUT]}\n\n"
                                   "Decide whether to delegate to the next sub-agent (e.g. worker or reviewer) or give the final Korean answer."
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": str(out)[:MAX_TOOL_OUTPUT]
                    })
        else:
            print(f"\n⚠️ 오케스트레이터 최대 단계({MAX_ORCHESTRATOR_STEPS}회) 도달")

if __name__ == "__main__":
    orchestrator_loop()
