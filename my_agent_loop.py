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

# Windows 콘솔(cp949)에서 한글/이모지 출력이 깨지거나 예외가 나지 않도록 UTF-8로 고정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 1. 실행 중인 llama-server.exe (127.0.0.1:8000) 연결 설정
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="local-key"
)

MODEL_NAME = "qwen2.5-coder-3b-instruct"

MAX_STEPS = 8
MAX_HISTORY = 24
MAX_TOOL_OUTPUT = 6000

# 프로젝트 지침 자동 로드 설정
PROJECT_DOC_FILES = ["AGENT.md", "CLAUDE.md", "README.md", "SETUP.md"]
MAX_DOC_CHARS_PER_FILE = 1200
MAX_PROJECT_DOC_CHARS = 3000

# 탐색 도구(list_dir/glob/grep) 출력 및 검색 범위 제한
MAX_LIST_ENTRIES = 200
MAX_GLOB_RESULTS = 200
MAX_GREP_RESULTS = 120
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_LINE_CHARS = 300
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode"}

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
            "name": "list_dir",
            "description": "List files and subdirectories of a directory. Directories end with '/'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current directory)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files/directories by glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern such as '**/*.py'"},
                    "path": {"type": "string", "description": "Base directory to search from (default: current directory)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression and return matching paths, line numbers, and lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "File or directory to search (default: current directory)"},
                    "include": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py' (default: '*')"}
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
            "description": "Replace one exact occurrence of old_str with new_str in an existing file. Use this for partial edits instead of rewriting the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_str": {"type": "string", "description": "Exact existing text to replace (must be unique in the file)"},
                    "new_str": {"type": "string", "description": "New text to put in place of old_str"}
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
                "properties": {
                    "command": {"type": "string", "description": "Windows PowerShell command to execute"}
                },
                "required": ["command"]
            }
        }
    }
]

# 3. 위험 명령어 차단 필터 (대소문자 무시)
DANGEROUS_PATTERNS = [
    (r"remove-item\b(?=[^\n]*-recurse)(?=[^\n]*-force)", "Remove-Item -Recurse -Force"),
    (r"\b(?:rm|del|erase|rd|rmdir)\b[^\n]*\s-r(?:ec(?:urse)?)?\b[^\n]*\s-fo(?:rce)?\b", "recursive force delete alias"),
    (r"\bformat-volume\b", "Format-Volume"),
    (r"\bclear-disk\b", "Clear-Disk"),
    (r"\binitialize-disk\b", "Initialize-Disk"),
    (r"\bformat\s+[a-z]:", "format <drive>:"),
    (r"\bmkfs(?:\.\w+)?\b", "mkfs"),
    (r"\bdiskpart\b", "diskpart"),
    (r"\bcipher\s+/w", "cipher /w (free space wipe)"),
    (r"\bstop-computer\b", "Stop-Computer"),
    (r"\brestart-computer\b", "Restart-Computer"),
    (r"\bshutdown\b", "shutdown"),
]

def check_dangerous_command(command: str):
    """치명적일 수 있는 명령어를 사전에 감지해 라벨을 반환 (안전하면 None)"""
    for pattern, label in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return label
    return None

# 4. 프로젝트 지침 파일 자동 로드
def load_project_docs(base_dir: str) -> str:
    """현재 폴더의 AGENT.md/CLAUDE.md/README.md/SETUP.md를 읽어 시스템 프롬프트용 문맥으로 만든다."""
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
        snippet = text[:limit]
        if len(text) > limit:
            snippet += "\n...(truncated)"
        sections.append(f"--- {filename} ---\n{snippet}")
        remaining -= limit

    if not sections:
        return ""
    return "Project instructions (auto-loaded, may be truncated):\n" + "\n\n".join(sections)

# 5. 탐색 도구 헬퍼 (list_dir / glob / grep)
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
        return f"Error: cannot list '{path}': {e}"

    if not names:
        return f"Directory '{path}' is empty."

    names.sort(key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))
    lines = []
    for name in names[:MAX_LIST_ENTRIES]:
        full = os.path.join(target, name)
        lines.append(name + "/" if os.path.isdir(full) else name)
    if len(names) > MAX_LIST_ENTRIES:
        lines.append(f"... ({len(names) - MAX_LIST_ENTRIES} more entries truncated)")
    return f"Directory: {path}\n" + "\n".join(lines)

def glob_tool(pattern: str, path: str = ".") -> str:
    base = os.path.abspath(path or ".")
    if not os.path.isdir(base):
        return f"Error: '{path}' is not a directory."
    try:
        matches = glob_module.glob(pattern, root_dir=base, recursive=True)
    except (ValueError, re.error) as e:
        return f"Error: invalid glob pattern '{pattern}': {e}"

    results = []
    for match in matches:
        if set(match.replace("\\", "/").split("/")) & SKIP_DIRS:
            continue
        full = os.path.join(base, match)
        suffix = "/" if os.path.isdir(full) else ""
        results.append(match.replace("\\", "/") + suffix)

    results = sorted(set(results))
    if not results:
        return f"No matches for '{pattern}' under '{path}'."
    lines = results[:MAX_GLOB_RESULTS]
    if len(results) > MAX_GLOB_RESULTS:
        lines.append(f"... ({len(results) - MAX_GLOB_RESULTS} more matches truncated)")
    return f"Glob '{pattern}' under '{path}':\n" + "\n".join(lines)

def _iter_grep_files(base: str, include: str):
    if os.path.isfile(base):
        yield base
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for filename in sorted(files):
            if fnmatch.fnmatch(filename, include):
                yield os.path.join(root, filename)

def grep_tool(pattern: str, path: str = ".", include: str = "*") -> str:
    base = os.path.abspath(path or ".")
    if not os.path.exists(base):
        return f"Error: '{path}' does not exist."
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regular expression '{pattern}': {e}"

    include = include or "*"
    rel_base = base if os.path.isdir(base) else os.path.dirname(base)
    matches = []
    scanned = 0
    limit_hit = False

    for fpath in _iter_grep_files(base, include):
        scanned += 1
        try:
            if os.path.getsize(fpath) > MAX_GREP_FILE_BYTES or _is_binary_file(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if not regex.search(line):
                        continue
                    text = line.rstrip("\r\n")
                    if len(text) > MAX_GREP_LINE_CHARS:
                        text = text[:MAX_GREP_LINE_CHARS] + "..."
                    matches.append(f"{_format_path(rel_base, fpath)}:{lineno}: {text}")
                    if len(matches) >= MAX_GREP_RESULTS:
                        limit_hit = True
                        break
        except OSError:
            continue
        if limit_hit:
            break

    if not matches:
        return f"No matches for '{pattern}' under '{path}' (include='{include}')."
    header = f"Grep '{pattern}' under '{path}' (include='{include}', {scanned} files scanned):"
    if limit_hit:
        matches.append(f"... (stopped at {MAX_GREP_RESULTS} matches)")
    return header + "\n" + "\n".join(matches)

# 6. 로컬 도구 실행기 (PowerShell UTF-8 인코딩 및 사용자 승인 포함)
def execute_tool(name: str, args: dict) -> str:
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
            confirm = input(f"\n⚠️ [승인] '{args['path']}' 파일을 저장할까요? (y/n): ")
            if confirm.lower() != "y":
                return "User rejected file write."
            with open(args["path"], "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"Success: '{args['path']}' saved."

        elif name == "edit_file":
            path = args["path"]
            old_str = args["old_str"]
            new_str = args["new_str"]

            if not old_str:
                return "Error: 'old_str' must not be empty."

            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            occurrences = content.count(old_str)
            if occurrences == 0:
                return f"Error: 'old_str' was not found in '{path}'. Read the file again and copy the exact text."
            if occurrences > 1:
                return f"Error: 'old_str' appears {occurrences} times in '{path}'. Include more surrounding context so it is unique."

            confirm = input(f"\n⚠️ [승인] '{path}' 파일을 부분 수정할까요? (y/n): ")
            if confirm.lower() != "y":
                return "User rejected file edit."

            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(old_str, new_str, 1))
            return f"Success: '{path}' updated (1 replacement)."

        elif name == "run_powershell":
            cmd = args["command"]
            danger = check_dangerous_command(cmd)
            if danger:
                print(f"\n🚫 [차단] 위험 명령어 감지: {danger}\nPS> {cmd}")
                return (f"BLOCKED by safety filter: the command matches a destructive pattern ({danger}). "
                        "It was NOT executed. Use a safer, non-destructive approach.")

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

# 7. 컨텍스트 관리 함수
def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY:
        return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

# 8. 텍스트 JSON 출력 폴백 파서
def looks_like_tool_call(content: str) -> bool:
    """일반 텍스트 안에 도구 호출 JSON 의도가 보이는지 판별 (파싱 실패 피드백용)"""
    if not content:
        return False
    return bool(re.search(r'"name"\s*:', content) and re.search(r'"arguments"\s*:', content))

def _repair_pseudo_json(text: str) -> str:
    """경량 모델이 코드를 백틱/삼중따옴표로 감싸 만든 비정상 JSON을 표준 JSON으로 복구한다."""
    def replace_value(match: re.Match) -> str:
        key = json.dumps(match.group(1))
        value = json.dumps(match.group(2), ensure_ascii=False)
        return f"{key}: {value}"

    text = re.sub(
        r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"""(.*?)"""',
        replace_value, text, flags=re.DOTALL,
    )
    text = re.sub(
        r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*```(.*?)```',
        replace_value, text, flags=re.DOTALL,
    )
    text = re.sub(
        r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*`(.*?)`',
        replace_value, text, flags=re.DOTALL,
    )
    return text

def _json_unescape_relaxed(raw: str) -> str:
    """JSON 문자열 본문을 관대하게 디코드한다. (알 수 없는 이스케이프는 그대로 유지)"""
    simple = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
    out = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < len(raw):
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(char)
        i += 1
    return "".join(out)

def _repair_unescaped_quotes(text: str) -> str:
    """긴 코드 문자열(content) 안의 이스케이프되지 않은 따옴표를 표준 JSON으로 재인코딩한다.

    경량 모델이 write_file 인자에서 코드 일부의 따옴표만 이스케이프해 JSON이 깨지는 경우를 복구한다.
    content가 마지막 키일 때만 적용한다.
    """
    match = re.search(r'"content"\s*:\s*"', text)
    if not match:
        return text

    start = match.end()
    end = text.rfind('"')
    if end <= start:
        return text
    if not re.fullmatch(r"\s*}\s*}\s*", text[end + 1:]) and not re.fullmatch(r"\s*}\s*", text[end + 1:]):
        return text

    decoded = _json_unescape_relaxed(text[start:end])
    escaped = json.dumps(decoded, ensure_ascii=False)[1:-1]
    return text[:start] + escaped + text[end:]

def extract_fallback_tool_calls(content: str) -> list:
    """llama-server가 tool_calls 대신 일반 텍스트(```json)로 뱉었을 때 추출하는 보조 함수

    비정상 JSON(Python triple-quote/백틱 문자열, 미이스케이프 따옴표)은 복구 후 재파싱을 시도한다.
    """
    if not content:
        return []

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = match.group(1) if match else content.strip()

    data = None
    base_candidates = [json_str, _repair_pseudo_json(json_str)]
    candidates = list(base_candidates)
    for candidate in base_candidates:
        repaired = _repair_unescaped_quotes(candidate)
        if repaired not in candidates:
            candidates.append(repaired)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except Exception:
            pass
    if data is None:
        for candidate in candidates:
            try:
                data = ast.literal_eval(candidate)
                break
            except Exception:
                pass
    if data is None:
        return []

    if isinstance(data, dict) and "name" in data and "arguments" in data:
        return [{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "name": data["name"],
            "arguments": data["arguments"] if isinstance(data["arguments"], str) else json.dumps(data["arguments"], ensure_ascii=False)
        }]
    return []

# 9. 에이전트 루프
def build_system_prompt(current_dir: str, project_docs: str = None) -> str:
    """현재 디렉토리와 자동 로드한 프로젝트 지침을 포함한 시스템 프롬프트 생성"""
    if project_docs is None:
        project_docs = load_project_docs(current_dir)

    return f"""You are an autonomous Windows CLI coding agent.
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
1. list_dir(path): List files and subdirectories (directories end with '/').
2. glob(pattern, path): Find paths by glob pattern, e.g. '**/*.py'.
3. grep(pattern, path, include): Search file contents by regex; returns path:line: text.
4. read_file(path): Read a file.
5. write_file(path, content): Write or overwrite a file.
6. edit_file(path, old_str, new_str): Replace one unique occurrence in an existing file (preferred for partial edits).
7. run_powershell(command): Run a Windows PowerShell command in {current_dir}.

Prefer list_dir/glob/grep to explore the project before reading files or guessing paths.

Destructive commands (recursive force delete, disk format, shutdown, etc.) are blocked by a safety filter, so never rely on them.
Never fabricate tool results (for example, claiming a file was saved) — only real tool output is valid.
After each tool result, continue with the next required step until the task is complete.
When the task is complete, reply with a plain-text Korean summary only (no JSON, no tool call). There is no "report" tool.
Always reply to the user in Korean AFTER you receive the tool execution result.

{project_docs}"""

def agent_loop():
    current_dir = os.getcwd()  # 실제 현재 작업 경로 획득
    project_docs = load_project_docs(current_dir)
    system_prompt = build_system_prompt(current_dir, project_docs)

    messages = [{"role": "system", "content": system_prompt}]

    print("🤖 llama-server(127.0.0.1:8000) Windows 코딩 에이전트 연결 완료 (종료: exit)")
    if project_docs:
        print(f"📚 프로젝트 지침 자동 로드 완료 ({len(project_docs)}자)")
    else:
        print("📚 자동 로드할 프로젝트 지침 파일이 없습니다.")

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
            using_text_fallback = False
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
                    using_text_fallback = True
                    # 모델이 실제로 출력한 텍스트를 그대로 대화에 남겨 다음 턴의 형식을 유지시킨다.
                    messages.append({"role": "assistant", "content": assistant_msg.content})
                else:
                    # 3) 도구 호출 의도는 보이지만 파싱에 실패한 경우 자가 수정 요청
                    if looks_like_tool_call(assistant_msg.content):
                        print(f"\n⚠️ [LOG] Step {step}: Tool call JSON 파싱 실패 → 자가 수정 요청")
                        messages.append({"role": "assistant", "content": assistant_msg.content})
                        messages.append({
                            "role": "user",
                            "content": "[JSON parse error] Your tool call was not valid JSON. "
                                       "Re-send exactly one valid JSON object with \"name\" and \"arguments\". "
                                       "Escape newlines inside string values as \\n and quotes as \\\", "
                                       "and do not use triple quotes."
                        })
                        continue

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

                if using_text_fallback:
                    messages.append({
                        "role": "user",
                        "content": f"[Tool result: {func_name}]\n{str(tool_output)[:MAX_TOOL_OUTPUT]}\n\n"
                                   "Continue with the next step of the user's request using a tool call. "
                                   "Do not repeat a tool call that already succeeded."
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": str(tool_output)[:MAX_TOOL_OUTPUT]
                    })
        else:
            print(f"\n⚠️ 최대 작업 단계({MAX_STEPS}회)에 도달하여 루프를 중단했습니다.")

if __name__ == "__main__":
    agent_loop()
