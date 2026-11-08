"""시나리오 3: edit_file 부분 수정 통합 테스트

write_file로 버그가 있는 샘플 코드를 만들고, edit_file로 해당 줄만 수정한 뒤,
run_powershell로 실행해 결과가 바뀌는지 검증한다.

실행: python test_edit_file.py
"""
import builtins
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import my_agent_loop as agent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ORIGINAL_CODE = '''def add(a, b):
    return a - b  # BUG: subtraction instead of addition


if __name__ == "__main__":
    print("2 + 3 =", add(2, 3))
'''

BUGGY_LINE = "    return a - b  # BUG: subtraction instead of addition"
FIXED_LINE = "    return a + b"


def with_answer(answer, fn, *args, **kwargs):
    """execute_tool의 input() 승인 프롬프트를 자동 응답으로 대체한다."""
    original_input = builtins.input
    builtins.input = lambda *_: answer
    try:
        return fn(*args, **kwargs)
    finally:
        builtins.input = original_input


def run_python(path):
    return with_answer("y", agent.execute_tool, "run_powershell", {"command": f'python "{path}"'})


def main():
    workdir = tempfile.mkdtemp(prefix="agent_edit_test_")
    path = os.path.join(workdir, "calc.py")
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(("PASS" if ok else "FAIL"), "-", name)
        if not ok and detail:
            print("       ", detail.strip().replace("\n", " | ")[:300])

    try:
        # 1) write_file로 샘플 코드 생성
        out = with_answer("y", agent.execute_tool, "write_file", {"path": path, "content": ORIGINAL_CODE})
        check("write_file로 샘플 생성", os.path.isfile(path) and "Success" in out, out)

        # 2) 수정 전 실행: 버그로 인해 -1이 출력되는지 확인
        out = run_python(path)
        check("수정 전 실행 결과가 -1 (버그 재현)", "2 + 3 = -1" in out, out)

        # 3) edit_file로 버그 줄만 부분 수정
        out = with_answer("y", agent.execute_tool, "edit_file", {
            "path": path,
            "old_str": BUGGY_LINE,
            "new_str": FIXED_LINE,
        })
        check("edit_file이 1회 치환 성공", "Success" in out, out)

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        check(
            "나머지 코드는 유지되고 버그 줄만 수정됨",
            FIXED_LINE in content and BUGGY_LINE not in content and content.count("def add") == 1,
            content,
        )

        # 4) 수정 후 실행: 5가 출력되는지 확인
        out = run_python(path)
        check("수정 후 실행 결과가 5", "2 + 3 = 5" in out, out)

        # 5) 존재하지 않는 old_str: 에러 피드백 + 파일 불변
        before = content
        out = with_answer("y", agent.execute_tool, "edit_file", {
            "path": path,
            "old_str": "no_such_line_xyz",
            "new_str": "x",
        })
        with open(path, "r", encoding="utf-8") as f:
            after = f.read()
        check(
            "미존재 old_str은 에러 반환 및 파일 불변",
            "not found" in out and after == before,
            out,
        )

        # 6) 중복 old_str: 에러 피드백 + 파일 불변
        dup_path = os.path.join(workdir, "dup.txt")
        with_answer("y", agent.execute_tool, "write_file", {"path": dup_path, "content": "x\nx\n"})
        out = with_answer("y", agent.execute_tool, "edit_file", {
            "path": dup_path,
            "old_str": "x",
            "new_str": "y",
        })
        with open(dup_path, "r", encoding="utf-8") as f:
            dup_content = f.read()
        check(
            "중복 old_str은 에러 반환 및 파일 불변",
            "appears 2 times" in out and dup_content == "x\nx\n",
            out,
        )

        # 7) 사용자 거절: 파일 불변
        out = with_answer("n", agent.execute_tool, "edit_file", {
            "path": path,
            "old_str": FIXED_LINE,
            "new_str": "    return a * b",
        })
        with open(path, "r", encoding="utf-8") as f:
            rejected_content = f.read()
        check(
            "사용자 거절 시 파일 불변",
            "rejected" in out and rejected_content == before,
            out,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok in results if ok)
    total = len(results)
    print(f"\n결과: {passed}/{total} 통과")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
