"""실제 로컬 LLM API(llama-server)를 호출하는 통합 테스트 러너

- 요구사항 프롬프트를 prompts/*.txt에서 읽어 실제 API 호출에 사용
- 매 스텝의 LLM 요청/응답과 도구 실행 결과를 텍스트 로그로 저장
- 테스트 시작/종료 및 스텝별 요청/응답 시각을 기록
- 결과: test_results/<scenario>_YYYYMMDD_HHMMSS.txt

실행:
    python test_live_llm.py 2   # 시나리오 2: 계산기 모듈 생성 및 실행 검증
    python test_live_llm.py 3   # 시나리오 3: edit_file 부분 수정 (기본값)
"""
import builtins
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import my_agent_loop as agent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
RESULT_DIR = os.path.join(BASE_DIR, "test_results")
API_MODELS_URL = "http://127.0.0.1:8000/v1/models"

BUGGY_CODE = '''def add(a, b):
    return a - b  # BUG: subtraction instead of addition


if __name__ == "__main__":
    print("2 + 3 =", add(2, 3))
'''

DIV_ZERO_CHECK = """try:
    import calc

    calc.divide(1, 0)
    print("NO_ERROR")
except ValueError:
    print("VALUE_ERROR")
"""


def now_text() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def check_server() -> bool:
    try:
        with urllib.request.urlopen(API_MODELS_URL, timeout=5) as res:
            return res.status == 200
    except Exception:
        return False


def format_message(message: dict) -> str:
    role = message.get("role", "?")
    parts = [f"[{role}]"]
    content = message.get("content")
    if content:
        parts.append(str(content))
    if message.get("tool_calls"):
        parts.append("tool_calls=" + json.dumps([
            {
                "id": call.get("id"),
                "name": call.get("function", {}).get("name"),
                "arguments": call.get("function", {}).get("arguments"),
            }
            for call in message["tool_calls"]
        ], ensure_ascii=False))
    if message.get("tool_call_id"):
        parts.append(f"(tool_call_id={message['tool_call_id']})")

    text = " ".join(parts)
    if len(text) > 1500:
        text = text[:1500] + f"... (truncated, total {len(text)} chars)"
    return text


def read_workspace_file(workspace: str, filename: str) -> str:
    try:
        with open(os.path.join(workspace, filename), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 시나리오 2: 계산기 모듈 생성 및 실행 검증
# ---------------------------------------------------------------------------
def setup_scenario2(workspace: str, w) -> None:
    w("[테스트 준비] 빈 작업 디렉토리에서 계산기 모듈을 처음부터 생성")
    w(f"준비 시각: {now_text()}")
    w()


def verify_scenario2(workspace: str, w, used_tools: list) -> list:
    checks = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))

    calc_code = read_workspace_file(workspace, "calc.py")
    test_code = read_workspace_file(workspace, "test_calc.py")

    w("--- calc.py ---")
    w(calc_code or "<파일 없음>")
    w("--- test_calc.py ---")
    w(test_code or "<파일 없음>")

    check("calc.py 생성됨", bool(calc_code.strip()))
    check("test_calc.py 생성됨", bool(test_code.strip()))
    check(
        "add/subtract/multiply/divide 함수 정의됨",
        all(re.search(rf"def\s+{name}\s*\(", calc_code) for name in ("add", "subtract", "multiply", "divide")),
    )
    check("0 나누기 ValueError 코드 포함", "ValueError" in calc_code)
    check("write_file 도구 사용", "write_file" in used_tools)

    test_output = agent.execute_tool("run_powershell", {"command": "python test_calc.py"})
    w("[python test_calc.py 실행 결과]")
    w(test_output)
    check("테스트 실행 결과에 ALL TESTS PASSED 포함", "ALL TESTS PASSED" in test_output)

    # 에이전트가 만든 테스트와 별개로 모듈을 직접 import해 계산 결과를 검증
    import_output = agent.execute_tool("run_powershell", {
        "command": 'python -c "import calc; print(calc.add(2,3), calc.subtract(7,4), calc.multiply(6,7), calc.divide(8,2))"'
    })
    w("[독립 검증: 모듈 직접 import]")
    w(import_output)
    check("독립 계산 결과 5 3 42 4", "5 3 42 4" in import_output)

    div_path = os.path.join(workspace, "_verify_div.py")
    with open(div_path, "w", encoding="utf-8") as f:
        f.write(DIV_ZERO_CHECK)
    div_output = agent.execute_tool("run_powershell", {"command": "python _verify_div.py"})
    w("[독립 검증: 0으로 나누기]")
    w(div_output)
    check("0으로 나누면 ValueError 발생", "VALUE_ERROR" in div_output and "NO_ERROR" not in div_output)

    return checks


# ---------------------------------------------------------------------------
# 시나리오 3: edit_file 부분 수정
# ---------------------------------------------------------------------------
def setup_scenario3(workspace: str, w) -> None:
    setup_result = agent.execute_tool("write_file", {"path": "calc_demo.py", "content": BUGGY_CODE})
    w("[테스트 준비] 버그가 있는 calc_demo.py 생성")
    w(f"준비 시각: {now_text()}")
    w(setup_result)
    w()


def verify_scenario3(workspace: str, w, used_tools: list) -> list:
    checks = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))

    final_code = read_workspace_file(workspace, "calc_demo.py")
    w("--- calc_demo.py 최종 내용 ---")
    w(final_code)

    run_output = agent.execute_tool("run_powershell", {"command": "python calc_demo.py"})
    w("[python calc_demo.py 실행 결과]")
    w(run_output)

    check("edit_file 도구 사용", "edit_file" in used_tools)
    check("버그 줄이 덧셈으로 수정됨", "return a + b" in final_code)
    check("뺄셈 버그 코드 제거됨", "return a - b" not in final_code)
    check("파일 전체 재작성 아님 (def add 1회)", final_code.count("def add") == 1)
    check("실행 결과 2 + 3 = 5", "2 + 3 = 5" in run_output)

    return checks


SCENARIOS = {
    "2": {
        "name": "시나리오 2: 계산기 모듈 생성 및 실행 검증",
        "prompt": "scenario2_calculator.txt",
        "prefix": "scenario2",
        "setup": setup_scenario2,
        "verify": verify_scenario2,
    },
    "3": {
        "name": "시나리오 3: edit_file 부분 수정",
        "prompt": "scenario3_edit_file.txt",
        "prefix": "scenario3",
        "setup": setup_scenario3,
        "verify": verify_scenario3,
    },
}


def run_scenario(scenario_id: str) -> int:
    config = SCENARIOS[scenario_id]
    prompt_path = os.path.join(PROMPT_DIR, config["prompt"])

    if not check_server():
        print(f"❌ LLM API 서버에 연결할 수 없습니다: {API_MODELS_URL}")
        return 1
    if not os.path.isfile(prompt_path):
        print(f"❌ 요구사항 프롬프트 파일이 없습니다: {prompt_path}")
        return 1

    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt = f.read().strip()

    os.makedirs(RESULT_DIR, exist_ok=True)
    started = datetime.datetime.now()
    log_path = os.path.join(RESULT_DIR, f"{config['prefix']}_{started:%Y%m%d_%H%M%S}.txt")

    workspace = tempfile.mkdtemp(prefix=f"agent_live_{config['prefix']}_")
    original_cwd = os.getcwd()
    original_input = builtins.input
    builtins.input = lambda *_: "y"  # 테스트 자동 승인 (위험 명령어 필터는 그대로 동작)

    used_tools = []
    final_text = ""
    checks = []

    try:
        with open(log_path, "w", encoding="utf-8") as log:
            def w(line: str = ""):
                log.write(line + "\n")
                log.flush()

            w("=" * 72)
            w(f"로컬 LLM 통합 테스트 — {config['name']}")
            w(f"테스트 시작 일시: {started.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}")
            w(f"모델: {agent.MODEL_NAME}")
            w(f"API 엔드포인트: {agent.client.base_url}")
            w(f"요구사항 프롬프트 파일: {prompt_path}")
            w(f"작업 디렉토리: {workspace}")
            w("=" * 72)
            w("[요구사항 프롬프트 원문]")
            w(prompt)
            w("=" * 72)

            os.chdir(workspace)
            config["setup"](workspace, w)

            messages = [
                {"role": "system", "content": agent.build_system_prompt(workspace)},
                {"role": "user", "content": prompt},
            ]

            for step in range(1, agent.MAX_STEPS + 1):
                w("-" * 72)
                w(f"[Step {step}] LLM 요청 시각: {now_text()}")
                w("[LLM 요청 messages]")
                for message in messages:
                    w(format_message(message))

                try:
                    response = agent.client.chat.completions.create(
                        model=agent.MODEL_NAME,
                        messages=messages,
                        tools=agent.TOOLS,
                        temperature=0.2,
                    )
                except Exception as e:
                    w(f"[LLM 요청 실패] {e}")
                    break

                w(f"[LLM 응답 수신 시각] {now_text()}")
                choice = response.choices[0]
                assistant_msg = choice.message
                w(f"[finish_reason] {choice.finish_reason}")

                raw_dump = json.dumps(
                    assistant_msg.model_dump(exclude_unset=True),
                    ensure_ascii=False,
                    default=str,
                )
                w("[LLM 응답 원문(JSON)]")
                w(raw_dump if len(raw_dump) <= 5000 else raw_dump[:5000] + "...(truncated)")
                w()

                calls_to_run = []
                using_text_fallback = False
                if assistant_msg.tool_calls:
                    w("[파싱] 표준 tool_calls 감지")
                    messages.append(assistant_msg.model_dump(exclude_unset=True))
                    calls_to_run = [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in assistant_msg.tool_calls
                    ]
                else:
                    fallback_calls = agent.extract_fallback_tool_calls(assistant_msg.content)
                    if fallback_calls:
                        w("[파싱] 텍스트 JSON 폴백 감지")
                        calls_to_run = fallback_calls
                        using_text_fallback = True
                        messages.append({"role": "assistant", "content": assistant_msg.content})
                    else:
                        if agent.looks_like_tool_call(assistant_msg.content):
                            w("[파싱] Tool call JSON 파싱 실패 → 자가 수정 요청")
                            messages.append({"role": "assistant", "content": assistant_msg.content})
                            messages.append({
                                "role": "user",
                                "content": "[JSON parse error] Your tool call was not valid JSON. "
                                           "Re-send exactly one valid JSON object with \"name\" and \"arguments\". "
                                           "Escape newlines inside string values as \\n and quotes as \\\", "
                                           "and do not use triple quotes."
                            })
                            continue

                        final_text = assistant_msg.content or ""
                        w("[파싱] 도구 호출 없음 — 최종 텍스트 응답")
                        w(f"[최종 응답 내용]\n{final_text}")
                        messages.append({"role": "assistant", "content": assistant_msg.content})
                        break

                for call in calls_to_run:
                    func_name = call["name"]
                    raw_args = call["arguments"]
                    used_tools.append(func_name)
                    w(f"[도구 호출] {func_name}")
                    w(f"  arguments: {raw_args}")

                    try:
                        parsed_args = json.loads(raw_args)
                        w(f"  실행 시각: {now_text()}")
                        tool_output = agent.execute_tool(func_name, parsed_args)
                    except json.JSONDecodeError as e:
                        tool_output = f"JSON parse error: '{raw_args}' is not valid JSON ({e}). Fix the format and retry."
                    except Exception as e:
                        tool_output = f"Tool execution error: {e}"

                    w(f"[도구 결과: {func_name}]")
                    w(str(tool_output)[:agent.MAX_TOOL_OUTPUT])
                    w()

                    if using_text_fallback:
                        messages.append({
                            "role": "user",
                            "content": f"[Tool result: {func_name}]\n{str(tool_output)[:agent.MAX_TOOL_OUTPUT]}\n\n"
                                       "Continue with the next step of the user's request using a tool call. "
                                       "Do not repeat a tool call that already succeeded."
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": str(tool_output)[:agent.MAX_TOOL_OUTPUT],
                        })

                messages = agent.trim_context(messages)
            else:
                w(f"[중단] 최대 작업 단계({agent.MAX_STEPS}회)에 도달했습니다.")

            # 최종 검증
            w("=" * 72)
            w("[최종 검증]")
            w(f"검증 시각: {now_text()}")
            w("--- 작업 디렉토리 파일 목록 ---")
            w(agent.list_dir_tool(workspace))
            w()

            checks = config["verify"](workspace, w, used_tools)

            w("--- 검증 항목 ---")
            for name, ok in checks:
                w(f"{'PASS' if ok else 'FAIL'} - {name}")

            passed = sum(1 for _, ok in checks if ok)
            total = len(checks)
            overall = passed == total
            w()
            w(f"[최종 결과] {'PASS' if overall else 'FAIL'} ({passed}/{total})")
            w(f"테스트 종료 일시: {now_text()}")
            w(f"사용한 도구 순서: {used_tools}")
            w("=" * 72)

        print(f"로그 저장: {log_path}")
        print(f"최종 결과: {'PASS' if overall else 'FAIL'} ({passed}/{total})")
        if not overall:
            print("실패 항목:")
            for name, ok in checks:
                if not ok:
                    print(f" - {name}")
        return 0 if overall else 1
    finally:
        builtins.input = original_input
        os.chdir(original_cwd)
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    scenario_id = sys.argv[1] if len(sys.argv) > 1 else "3"
    if scenario_id not in SCENARIOS:
        print(f"사용법: python test_live_llm.py [{'|'.join(SCENARIOS)}]")
        return 2
    return run_scenario(scenario_id)


if __name__ == "__main__":
    sys.exit(main())
