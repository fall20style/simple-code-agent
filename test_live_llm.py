"""실제 로컬 LLM API(llama-server)를 호출하는 시나리오 3 통합 테스트

- 요구사항 프롬프트를 prompts/scenario3_edit_file.txt에서 읽어 호출에 사용
- 매 스텝의 LLM 요청/응답과 도구 실행 결과를 텍스트 로그로 저장
- 테스트 시작/종료 시각을 로그에 기록
- 결과: test_results/scenario3_YYYYMMDD_HHMMSS.txt

실행: python test_live_llm.py
"""
import builtins
import datetime
import json
import os
import shutil
import sys
import tempfile
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import my_agent_loop as agent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "scenario3_edit_file.txt")
RESULT_DIR = os.path.join(BASE_DIR, "test_results")
API_MODELS_URL = "http://127.0.0.1:8000/v1/models"

BUGGY_CODE = '''def add(a, b):
    return a - b  # BUG: subtraction instead of addition


if __name__ == "__main__":
    print("2 + 3 =", add(2, 3))
'''


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


def main() -> int:
    if not check_server():
        print(f"❌ LLM API 서버에 연결할 수 없습니다: {API_MODELS_URL}")
        return 1
    if not os.path.isfile(PROMPT_PATH):
        print(f"❌ 요구사항 프롬프트 파일이 없습니다: {PROMPT_PATH}")
        return 1

    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        prompt = f.read().strip()

    os.makedirs(RESULT_DIR, exist_ok=True)
    started = datetime.datetime.now()
    log_path = os.path.join(RESULT_DIR, f"scenario3_{started:%Y%m%d_%H%M%S}.txt")

    workspace = tempfile.mkdtemp(prefix="agent_live_")
    original_cwd = os.getcwd()
    original_input = builtins.input
    builtins.input = lambda *_: "y"  # 테스트 자동 승인 (위험 명령어 필터는 그대로 동작)

    used_tools = []
    final_text = ""

    try:
        with open(log_path, "w", encoding="utf-8") as log:
            def w(line: str = ""):
                log.write(line + "\n")
                log.flush()

            w("=" * 72)
            w("로컬 LLM 통합 테스트 — 시나리오 3: edit_file 부분 수정")
            w(f"테스트 시작 일시: {started.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}")
            w(f"모델: {agent.MODEL_NAME}")
            w(f"API 엔드포인트: {agent.client.base_url}")
            w(f"요구사항 프롬프트 파일: {PROMPT_PATH}")
            w(f"작업 디렉토리: {workspace}")
            w("=" * 72)
            w("[요구사항 프롬프트 원문]")
            w(prompt)
            w("=" * 72)

            os.chdir(workspace)

            # 테스트 준비: 버그가 있는 샘플 파일을 실제 write_file 도구로 생성
            setup_result = agent.execute_tool("write_file", {"path": "calc_demo.py", "content": BUGGY_CODE})
            w("[테스트 준비] 버그가 있는 calc_demo.py 생성")
            w(f"준비 시각: {now_text()}")
            w(setup_result)
            w()

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
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": c["id"],
                                    "type": "function",
                                    "function": {"name": c["name"], "arguments": c["arguments"]},
                                }
                                for c in fallback_calls
                            ],
                        })
                    else:
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

            try:
                with open("calc_demo.py", "r", encoding="utf-8") as f:
                    final_code = f.read()
            except OSError as e:
                final_code = f"<파일 읽기 실패: {e}>"

            w("--- calc_demo.py 최종 내용 ---")
            w(final_code)

            run_output = agent.execute_tool("run_powershell", {"command": "python calc_demo.py"})
            w("[python calc_demo.py 실행 결과]")
            w(run_output)

            checks = [
                ("edit_file 도구 사용", "edit_file" in used_tools),
                ("버그 줄이 덧셈으로 수정됨", "return a + b" in final_code),
                ("뺄셈 버그 코드 제거됨", "return a - b" not in final_code),
                ("파일 전체 재작성 아님 (def add 1회)", final_code.count("def add") == 1),
                ("실행 결과 2 + 3 = 5", "2 + 3 = 5" in run_output),
            ]

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


if __name__ == "__main__":
    sys.exit(main())
