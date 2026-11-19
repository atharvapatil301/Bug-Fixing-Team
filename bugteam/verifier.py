"""The verifier, a deep agent that runs the fix in a sandbox.

It gets the issue and the name of the changed file, but not the change itself, so that its test
comes from the issue and the docstring. It ends its run by calling one of two handoff tools.
"""
from deepagents import create_deep_agent
from langchain.messages import HumanMessage
from langchain.tools import tool
from langgraph.errors import GraphRecursionError

from . import config
from .repo import issue_text, repo_files
from .sandbox import open_sandbox, read_sandbox_file, upload_files

# Two rules in this prompt come from failures seen in testing:
# - "Only look for files under {workdir}": the deep agent's glob tool once searched "/" instead of
#   the working directory.
# - "If the issue's suggestion contradicts the docstring, follow the docstring": in the experiment,
#   the verifier tested an issue reporter's suggested fix that the docstring contradicts, and
#   accepted a wrong fix.
VERIFIER_PROMPT = """You check a proposed fix for one issue in huskyutils, a small Python library.
Another agent wrote the fix. You are not shown the change, so that your tests come from the issue
and the docstring and not from the fix. Your files are under {workdir}:

- {workdir}/original/ is the repository before the fix
- {workdir}/fixed/ is the repository with the fix

Only look for files under {workdir}: always pass path="{workdir}" to glob, grep and ls. Never search /.

Run Python with execute. The tests use unittest. To run them, cd into one of the two folders and run
`python -m unittest discover -s tests -v`.

1. Read the issue, and the docstring of the function it is about. The docstring is the specification.
2. Write tests/test_issue.py in both folders. It checks what the issue and the docstring say the
   function should do, including every example in the docstring. Write it from the issue and the
   docstring, not from the fix. Test only inputs the docstring allows: if it says a value is at
   least 1, do not test 0. 
3. If the issue's suggestion contradicts the docstring, follow the docstring
4. Run all the tests in both folders. Your test should fail in original/, which shows it reproduces
   the issue; that failure is what you want, not a reason to reject the fix. In fixed/, every test
   should pass, yours and the ones that were already there.

Then call exactly one of these tools, and stop:
- accept_fix, if your test fails in original/ and every test passes in fixed/. Give the commands you
  ran and the result.
- transfer_to_fixer, otherwise. Give the failing test and its output, so that the fixer can see
  what is wrong. If your test passes in original/, it does not reproduce the issue: correct your
  test before you decide."""


@tool(return_direct=True)
def accept_fix(evidence: str) -> str:
    """Accept the fix. evidence: the commands you ran and what they showed, briefly."""
    return "Fix accepted."

@tool(return_direct=True)
def transfer_to_fixer(reason: str) -> str:
    """Send the fix back to the fixer. reason: what is wrong, with the failing test and its output."""
    return "Sent back to the fixer."


def decision(messages):
    """('accept', evidence) or ('reject', reason) from the verifier's last handoff call, or
    ('none', '') if it stopped without calling either."""
    message_list = reversed(messages)
    for m in message_list:
        for call in getattr(m, "tool_calls", None) or []:
            if call["name"]=="accept_fix":
                return ("accept", call["args"]["evidence"])
            if call["name"]=="transfer_to_fixer":
                return ("reject", call["args"]["reason"])
        
    return ("none","")


# The docstring gate. The prompt's instruction to test every docstring example did not hold in
# the experiment, so it is enforced in code: an accept only stands if every `f(...) == expected`
# example in the original file's docstrings passes against fixed/.
import ast

def docstring_examples(source):
    """Every `f(...) == expected` line in the module's docstrings, as normalized source.

    Each example is rebuilt from its syntax tree with ast.unparse, so a trailing comment such as
    `# Monday` is dropped instead of being pasted into the check script, where it would comment out
    the closing parenthesis and make every example "fail"."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for line in (ast.get_docstring(node) or "").splitlines():
                try:
                    expr = ast.parse(line.strip(), mode="eval").body
                except SyntaxError:
                    continue
                if (isinstance(expr, ast.Compare) and len(expr.ops) == 1
                        and isinstance(expr.ops[0], ast.Eq) and isinstance(expr.left, ast.Call)):
                    out.append(ast.unparse(expr))
    return out


def module_imports(source):
    """The module's top-level import statements, so the examples can use names such as `date`
    even if the fix changed how the module imports them."""
    return [ast.unparse(n) for n in ast.parse(source).body if isinstance(n, (ast.Import, ast.ImportFrom))]


# Names that docstring examples use without the module importing them, e.g. `date` in dates.py,
# whose code only imports timedelta.
EXAMPLE_NAMES = "from datetime import date, datetime, time, timedelta"


def check_script(module, examples, imports=()):
    """A script that prints every docstring example the code fails, with what it got.

    Every example runs in its own try block, so the only way the script itself fails at run time is
    the fixed module failing to import, which is a real failure of the fix. A NameError for anything
    other than the function under test means the gate cannot evaluate that example, so it is skipped
    rather than counted against the fix; a NameError for the function itself means the fix removed
    it, and counts."""
    lines = [EXAMPLE_NAMES, *imports, f"from {module} import *", "bad = []"]
    for ex in examples:
        left = ast.parse(ex, mode="eval").body.left
        call, func = ast.unparse(left), ast.unparse(left.func)
        lines.append(f"try:\n    got = {call}\n    ok = ({ex})\n"
                     f"except NameError as e:\n"
                     f"    got, ok = repr(e), getattr(e, 'name', None) != {func!r}\n"
                     f"except Exception as e:\n"
                     f"    got, ok = repr(e), False\n"
                     f"if not ok: bad.append({ex!r} + '  # got ' + repr(got))")
    lines.append("print('\\n'.join(bad))")
    return "\n".join(lines)


def verify(issue, fix):
    """Run the verifier on one fix, in a new sandbox. fix is the fixer's transfer_to_verifier arguments.

    Returns {"decision": "accept" | "reject" | "none", "message": ..., "test": the test it wrote}."""
    files = repo_files()
    fixed = {**files, fix["path"]: fix["new_code"]}

    with open_sandbox() as (sandbox, workdir):
        upload_files(sandbox, files, f"{workdir}/original")
        upload_files(sandbox, fixed, f"{workdir}/fixed")

        verifier_agent = create_deep_agent(
            model=config.chat(),
            system_prompt=VERIFIER_PROMPT.format(workdir=workdir),
            tools=[accept_fix, transfer_to_fixer],
            backend=sandbox,
        )
        request = issue_text(issue) + f"\n\nThe fixer changed the file {fix['path']}."

        try:
            out = verifier_agent.invoke({"messages": [HumanMessage(content=request)]},
                                        {"recursion_limit": 80})
            verdict, message = decision(out["messages"])

            # Gate: an accept only stands if every docstring example holds in fixed/.
            if verdict == "accept" and fix["path"].endswith(".py"):
                original = files[fix["path"]]               # from ORIGINAL, so the fixer can't edit them away
                examples = docstring_examples(original)
                script = None
                if examples:
                    module = fix["path"][:-3].replace("/", ".")
                    script = check_script(module, examples, module_imports(original))
                    try:
                        compile(script, "check_docstring.py", "exec")
                    except SyntaxError:
                        script = None   # a bug in the gate, not in the fix: keep the verifier's verdict
                if script:
                    upload_files(sandbox, {"check_docstring.py": script}, f"{workdir}/fixed")
                    failed = sandbox.execute(f"cd {workdir}/fixed && python check_docstring.py 2>&1").output.strip()
                    if failed:
                        verdict, message = "reject", ("Your fix breaks examples in the docstring, which is the "
                                                    "specification:\n" + failed)
        except GraphRecursionError:
                verdict, message = "none", "The verifier ran out of steps before it accepted or rejected the fix."

        test = read_sandbox_file(sandbox, f"{workdir}/fixed/tests/test_issue.py")

    return {"decision": verdict, "message": message, "test": test} 
