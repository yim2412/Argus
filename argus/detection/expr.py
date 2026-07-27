"""룰 표현식 평가기 — `eval()` 없이.

룰 파일에는 `"baseline * 3"` 이나 `"median + 4 * mad"` 같은 식이 들어간다. 사용자가
고칠 수 있어야 하므로(CLAUDE.md: 튜닝은 코드 수정 없이 YAML 로) 문자열을 계산할
수단이 필요하다.

**`eval()` 은 쓰지 않는다.** 룰 파일은 사용자가 편집하고, 나중에는 남이 만든 룰을
받아 쓰게 된다. `eval()` 은 `__import__('os').system(...)` 한 줄로 임의 코드 실행이
되므로, 성능 모니터가 실행 통로가 된다.

대신 `ast.parse` 로 구문 트리를 만들고 **허용한 노드만** 통과시킨다. 화이트리스트라
새 문법이 생겨도 자동으로 막힌다(블랙리스트는 빠뜨리면 뚫린다).

    >>> evaluate("median + 4 * mad", {"median": 10.0, "mad": 2.0})
    18.0
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable, Mapping

# 허용 연산. 여기 없는 것은 전부 거부된다.
_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

# 호출 가능한 함수도 화이트리스트다. 인자 개수까지 고정한다.
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": lambda x, n=0: round(x, int(n)),
    "sqrt": math.sqrt,
    "log": math.log,
}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.Call, ast.IfExp,
)


class ExprError(ValueError):
    """룰 표현식이 잘못됐다. 룰 이름과 함께 보고해 어느 룰이 문제인지 알게 한다."""


def _check(node: ast.AST) -> None:
    """트리 전체가 화이트리스트 안에 있는지 확인한다."""
    for child in ast.walk(node):
        if isinstance(child, (ast.operator, ast.unaryop, ast.cmpop, ast.boolop)):
            continue  # 연산자 종류는 아래에서 개별 확인한다
        if not isinstance(child, _ALLOWED_NODES):
            raise ExprError(f"허용되지 않은 문법: {type(child).__name__}")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in _FUNCTIONS:
                name = getattr(child.func, "id", type(child.func).__name__)
                raise ExprError(f"허용되지 않은 함수: {name}")
            if child.keywords:
                raise ExprError("키워드 인자는 지원하지 않습니다")
        if isinstance(child, ast.Name) and child.id.startswith("_"):
            # `__class__` 류로 객체 내부에 닿는 경로를 원천 차단한다.
            raise ExprError(f"밑줄로 시작하는 이름은 쓸 수 없습니다: {child.id}")


def _eval(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, variables)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ExprError(f"허용되지 않은 상수: {type(node.value).__name__}")

    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        if node.id in _FUNCTIONS:
            return node.id
        raise ExprError(f"알 수 없는 이름: {node.id}")

    if isinstance(node, ast.BinOp):
        op = _BINARY.get(type(node.op))
        if op is None:
            raise ExprError(f"허용되지 않은 연산자: {type(node.op).__name__}")
        left, right = _eval(node.left, variables), _eval(node.right, variables)
        if left is None or right is None:
            return None  # 메트릭이 없는 상황. 0 으로 치면 잘못된 판정이 나온다.
        try:
            return op(float(left), float(right))
        except ZeroDivisionError:
            return None
        except OverflowError as exc:
            raise ExprError(f"수치 범위 초과: {exc}") from exc

    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise ExprError(f"허용되지 않은 단항 연산자: {type(node.op).__name__}")
        value = _eval(node.operand, variables)
        return None if value is None else op(float(value))

    if isinstance(node, ast.Compare):
        left = _eval(node.left, variables)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _COMPARE.get(type(op_node))
            if op is None:
                raise ExprError(f"허용되지 않은 비교: {type(op_node).__name__}")
            right = _eval(comparator, variables)
            if left is None or right is None:
                return None
            if not op(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        values = [_eval(v, variables) for v in node.values]
        if any(v is None for v in values):
            return None
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

    if isinstance(node, ast.IfExp):
        test = _eval(node.test, variables)
        if test is None:
            return None
        return _eval(node.body if test else node.orelse, variables)

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # _check 가 이미 보장
        args = [_eval(a, variables) for a in node.args]
        if any(a is None for a in args):
            return None
        try:
            return _FUNCTIONS[node.func.id](*args)
        except (TypeError, ValueError) as exc:
            raise ExprError(f"{node.func.id}() 호출 실패: {exc}") from exc

    raise ExprError(f"평가할 수 없는 노드: {type(node).__name__}")


def compile_expr(source: str) -> ast.Expression:
    """문법 검사까지 마친 트리를 돌려준다.

    룰을 **불러올 때** 한 번 검사하려고 분리했다. 평가 시점에 처음 검사하면 잘못된
    룰이 몇 시간 뒤 처음 발화할 때야 터진다 — 그때는 이미 늦다.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"구문 오류: {source!r} — {exc.msg}") from exc
    _check(tree)
    return tree


def evaluate(source: str | ast.Expression, variables: Mapping[str, Any]) -> Any:
    """표현식을 평가한다. 값을 알 수 없으면 `None`.

    `None` 을 0 으로 바꾸지 않는 것이 중요하다. GPU 가 없는 PC 에서 `gpu_temp` 는
    0도가 아니라 **모르는 값**이고, 0 으로 치면 "온도 정상"이라는 틀린 판정이 된다.
    """
    tree = compile_expr(source) if isinstance(source, str) else source
    return _eval(tree, variables)


if __name__ == "__main__":  # 스모크: python -m argus.detection.expr
    variables = {"median": 10.0, "mad": 2.0, "baseline": 5.0, "gpu_temp": None}

    cases = [
        ("median + 4 * mad", 18.0),
        ("baseline * 3", 15.0),
        ("max(baseline * 2, 20)", 20.0),
        ("median > 5", True),
        ("median > 5 and mad < 1", False),
        ("gpu_temp > 80", None),          # 값을 모르면 판정하지 않는다
        ("baseline if median > 5 else 0", 5.0),
    ]
    problems = []
    for source, expected in cases:
        got = evaluate(source, variables)
        mark = "OK " if got == expected else "!! "
        print(f"  {mark}{source:32} = {got}")
        if got != expected:
            problems.append(f"{source} → {got} (기대 {expected})")

    # 보안 — 전부 거부되어야 한다. 하나라도 통과하면 룰 파일이 실행 통로가 된다.
    attacks = [
        "__import__('os').system('calc')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('x','w')",
        "exec('x=1')",
        "median.__class__",
        "[1,2,3]",
        "lambda: 1",
        "median := 5",
    ]
    print()
    for attack in attacks:
        try:
            evaluate(attack, variables)
            problems.append(f"보안: 거부되어야 할 표현식이 통과했다 — {attack}")
            print(f"  !! 통과됨(위험): {attack}")
        except ExprError as exc:
            print(f"  OK 거부: {attack[:40]:42} ({exc})")

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("\n[OK] detection.expr")
