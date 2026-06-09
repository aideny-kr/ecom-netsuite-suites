# backend/app/services/metrics/expression_evaluator.py
"""Safe arithmetic evaluator over named metric leaves. No eval(); no names but metric keys."""

import ast


class ExpressionError(ValueError):
    pass


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)


def extract_dependencies(expression: str) -> list[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression: {e}") from e
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return sorted(names)


def validate_expression_operators(expression: str) -> None:
    """Author-time guard: raise ExpressionError if `expression` uses any node or operator
    that evaluate_expression would reject at compute time.

    Mirrors `_eval`'s allowlist EXACTLY (Add/Sub/Mult/Div binops, unary minus, numeric
    constants, names) but does NOT evaluate — leaf values are unknown at author time, so
    this validates structure only (e.g. it never rejects `a / b` for a possible runtime
    division-by-zero). An expression that passes here is structurally computable.

    Without this gate an author can persist a `status='active'` expression metric such as
    `a ** b` (power), `a % b` (mod), `a // b` (floor-div) or a comparison: it passes
    dependency validation, then raises ExpressionError on EVERY compute — a blessed,
    advertised-as-computable metric that can never produce a number.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression: {e}") from e

    def _check(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            _check(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            _check(node.left)
            _check(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            _check(node.operand)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return
        elif isinstance(node, ast.Name):
            return
        else:
            raise ExpressionError(f"disallowed token: {ast.dump(node)}")

    _check(tree)


def evaluate_expression(expression: str, resolved_values: dict[str, float]) -> float:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression: {e}") from e

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ExpressionError("division by zero")
            return left / right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in resolved_values:
                raise ExpressionError(f"missing dependency: {node.id}")
            return float(resolved_values[node.id])
        raise ExpressionError(f"disallowed token: {ast.dump(node)}")

    return _eval(tree)
