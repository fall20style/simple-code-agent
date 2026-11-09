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
import requests
from openai import OpenAI

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["no_proxy"] = "localhost,127.0.0.1"

USE_REMOTE_EXAONE = False
EXAONE_API_KEY = os.environ.get("EXAONE_API_KEY", "") 
EXAONE_URL = "https://exaone.ai"

client = None
MODEL_NAME = ""

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

AUTO_APPROVE = False
WORKSPACE_ROOT = os.path.abspath(".")

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


def stream_exaone_completion(messages: list, tools: list, temperature: float, max_tokens: int, label: str) -> dict:
    start_time = time.time()
    
    # [SSL 오류 해결] 방화벽 차단을 막기 위해 웹 브라우저(Chrome)인 것처럼 User-Agent를 추가합니다.
    headers = {
        "Authorization": f"Bearer {EXAONE_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    sanitized_messages = []
    for msg in messages:
        copied = dict(msg)
        if "tool_calls" in copied and copied["tool_calls"]:
            sanitized_tcs = []
            for tc in copied["tool_calls"]:
                tc_copy = dict(tc)
                if "function" in tc_copy:
                    func_copy = dict(tc_copy["function"])
                    if isinstance(func_copy.get("arguments"), dict):
                        func_copy["arguments"] = json.dumps(func_copy["arguments"], ensure_ascii=False)
                    tc_copy["function"] = func_copy
                sanitized_tcs.append(tc_copy)
            copied["tool_calls"] = sanitized_tcs
        sanitized_messages.append(copied)

    payload = {"model": MODEL_NAME, "messages": sanitized_messages, "temperature": temperature, "max_tokens": max_tokens, "stream": True}
    if tools:
        payload["tools"] = tools

    content_chunks, tool_calls_map, total_chars = [], {}, 0
    try:
        # 💡 [교정 파트] urllib3.errors를 urllib3.exceptions로 올바르게 정정하여 경고를 숨깁니다.
        import requests.packages.urllib3
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
        
        response = requests.post(url=EXAONE_URL, headers=headers, json=payload, stream=True, timeout=30, verify=False)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if not line: continue
            line_str = line.decode('utf-8').strip()
            if not line_str.startswith("data:"): continue
            data_body = line_str[5:].strip()
            if data_body == "[DONE]": break
            try:
                chunk = json.loads(data_body)
                if not chunk.get("choices"): continue
                delta = chunk["choices"].get("delta", {})
                if delta.get("content"):
                    text = delta["content"]
                    content_chunks.append(text)
                    total_chars += len(text)
                    if len(content_chunks) > 25:
                        tail = "".join(content_chunks[-25:])
                        if len(set(tail)) <= 2 and len(tail) >= 25:
                            print(f"\n⚠️ [{label}] 반복 출력 루프 차단.")
                            break
                if delta.get("tool_calls"):
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": "", "arguments": ""}}
                        if tc.get("id"): tool_calls_map[idx]["id"] = tc["id"]
                        if tc.get("function"):
                            func_info = tc["function"]
                            if func_info.get("name"): tool_calls_map[idx]["function"]["name"] += func_info["name"]
                            if func_info.get("arguments"):
                                tool_calls_map[idx]["function"]["arguments"] += func_info["arguments"]
                                total_chars += len(func_info["arguments"])
                elapsed = time.time() - start_time
                sys.stdout.write(f"\r  ⏳ [{label} 생성 중...] {total_chars}자 ({elapsed:.1f}초)   ")
                sys.stdout.flush()
            except Exception: continue
    except Exception as e:
        print(f"\n❌ 원격 API 요청 장애 발생: {e}")
        return {"content": f"원격 API 통신 에러: {e}", "tool_calls": []}

    if total_chars > 0:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    full_content = "".join(content_chunks)
    full_content = re.sub(r'<think>.*?</think>', '', full_content, flags=re.DOTALL).strip()
    tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] if tool_calls_map else []
    return {"content": full_content, "tool_calls": tool_calls}

def stream_local_chat_completion(messages: list, tools: list, temperature: float, max_tokens: int, label: str) -> dict:
    start_time = time.time()
    stream = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens, stream=True, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    content_chunks, tool_calls_map, total_chars = [], {}, 0
    for chunk in stream:
        if not chunk.choices: continue
        delta = chunk.choices.delta
        if delta.content:
            content_chunks.append(delta.content)
            total_chars += len(delta.content)
            if len(content_chunks) > 25:
                tail = "".join(content_chunks[-25:])
                if len(set(tail)) <= 2 and len(tail) >= 25: break
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": "", "arguments": ""}}
                if tc.id: tool_calls_map[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name: tool_calls_map[idx]["function"]["name"] += tc.function.name
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


def stream_local_chat_completion(messages: list, tools: list, temperature: float, max_tokens: int, label: str) -> dict:
    start_time = time.time()
    stream = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens, stream=True, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    content_chunks, tool_calls_map, total_chars = [], {}, 0
    for chunk in stream:
        if not chunk.choices: continue
        delta = chunk.choices.delta
        if delta.content:
            content_chunks.append(delta.content)
            total_chars += len(delta.content)
            if len(content_chunks) > 25:
                tail = "".join(content_chunks[-25:])
                if len(set(tail)) <= 2 and len(tail) >= 25: break
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": "", "arguments": ""}}
                if tc.id: tool_calls_map[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name: tool_calls_map[idx]["function"]["name"] += tc.function.name
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


def looks_like_tool_call(content: str) -> bool:
    return bool(content and re.search(r'"name"\s*:', content) and re.search(r'"arguments"\s*:', content))

def _repair_pseudo_json(text: str) -> str:
    def repl(m): return f"{json.dumps(m.group(1))}: {json.dumps(m.group(2), ensure_ascii=False)}"
    for pat in [r'"([A-Za-z_]\w*)"\s*:\s*"""(.*?)"""', r'"([A-Za-z_]\w*)"\s*:\s*```(.*?)```', r'"([A-Za-z_]\w*)"\s*:\s*`(.*?)`']:
        text = re.sub(pat, repl, text, flags=re.DOTALL)
    return text

def extract_fallback_tool_calls(content: str) -> list:
    if not content: return []
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = match.group(1) if match else content.strip()
    data = None
    for cand in [json_str, _repair_pseudo_json(json_str)]:
        try: data = json.loads(cand); break
        except Exception:
            try: data = ast.literal_eval(cand); break
            except Exception: pass
    if isinstance(data, dict) and "name" in data and "arguments" in data:
        return [{"id": f"call_{uuid.uuid4().hex[:8]}", "name": data["name"], "arguments": data["arguments"] if isinstance(data["arguments"], str) else json.dumps(data["arguments"], ensure_ascii=False)}]
    return []

def trim_context(messages: list) -> list:
    if len(messages) <= MAX_HISTORY: return messages
    return [messages[0]] + messages[-(MAX_HISTORY - 1):]

def build_single_agent_prompt(workspace_dir: str, project_docs: str) -> str:
    return f"""/no_think
You are an autonomous Single Coding Agent. Workspace: {workspace_dir}
1. Explore: `list_dir`, `glob`, `grep`, `read_file`.
2. Implement: `write_file`, `edit_file`. Prefer built-in `tkinter` for GUI.
3. Verify: `run_powershell`. Fix errors immediately.
4. Report: Provide final summary in Korean.
Call tools strictly via valid JSON structure.
{project_docs}"""

def single_agent_loop():
    project_docs = load_project_docs(WORKSPACE_ROOT)
    messages = [{"role": "system", "content": build_single_agent_prompt(WORKSPACE_ROOT, project_docs)}]
    while True:
        user_input = input("\n👤 사용자: ").strip()
        if not user_input: continue
        if user_input.lower() in ("exit", "quit", "종료"): break
        messages.append({"role": "user", "content": user_input})
        messages = trim_context(messages)

        for step in range(1, MAX_AGENT_STEPS + 1):
            if USE_REMOTE_EXAONE and step > 1: time.sleep(2.0)
            res = stream_exaone_completion(messages, tools=ALL_TOOLS, temperature=0.15, max_tokens=MAX_TOKENS_AGENT, label=f"agent Step {step}") if USE_REMOTE_EXAONE else stream_local_chat_completion(messages, tools=ALL_TOOLS, temperature=0.15, max_tokens=MAX_TOKENS_AGENT, label=f"agent Step {step}")
            content, raw_tcs = res["content"], res["tool_calls"]
            calls, using_fallback = [], False

            if raw_tcs:
                print(f"\n🔧 [Step {step}/{MAX_AGENT_STEPS}] 표준 Tool Calling"); messages.append({"role": "assistant", "tool_calls": raw_tcs, "content": content})
                calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in raw_tcs]
            else:
                fb = extract_fallback_tool_calls(content)
                if fb: print(f"\n⚠️ [Step {step}] Fallback 파싱"); calls = fb; using_fallback = True; messages.append({"role": "assistant", "content": content})
                elif looks_like_tool_call(content): messages.append({"role": "assistant", "content": content}); messages.append({"role": "user", "content": "[JSON parse error] Retry valid JSON. /no_think"}); continue
                else: print(f"\n💬 [Step {step}] 최종 응답 완료"); messages.append({"role": "assistant", "content": content}); print(f"🤖 에이전트: {content}"); break

            for call in calls:
                fname = call["name"]
                try:
                    pargs = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
                    out = execute_tool(fname, pargs)
                except Exception as e: out = f"Tool error: {e}"
                
                if using_fallback: messages.append({"role": "user", "content": f"[Tool result: {fname}]\n{str(out)[:MAX_TOOL_OUTPUT]}\nContinue. /no_think"})
                else: messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(out)[:MAX_TOOL_OUTPUT]})
        else: print(f"\n⚠️ 최대 단계 도달")

def parse_args():
    parser = argparse.ArgumentParser(description="Single Coding Agent")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--workspace", type=str, default=".")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--model", type=str, default="")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    AUTO_APPROVE = args.auto
    WORKSPACE_ROOT = os.path.abspath(args.workspace)
    USE_REMOTE_EXAONE = args.remote
    if USE_REMOTE_EXAONE:
        MODEL_NAME = args.model if args.model else "k-exaone_v2.5"
    else:
        MODEL_NAME = args.model if args.model else "Qwen3-4B-Q4_K_M.gguf"
        client = OpenAI(base_url="http://localhost:8000/v1", api_key="local-key")
    os.makedirs(WORKSPACE_ROOT, exist_ok=True); os.chdir(WORKSPACE_ROOT)
    single_agent_loop()
