"""Additional fixed holdouts. Restricted AST interpretation, never exec/eval."""
import ast
import math


DISCOUNT = 'def percent_off(price, percent):\n    return price - percent\n'
AVERAGE = 'def average(values):\n    return sum(values) / len(values)\n'


def expression_value(source, name, inputs):
    tree = ast.parse(source)
    if len(list(ast.walk(tree))) > 500 or any(not isinstance(n, ast.FunctionDef) for n in tree.body):
        raise ValueError('Unsupported or oversized fixture AST')
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(functions) != 1:
        raise ValueError('Missing/duplicate function')
    fn = functions[0]
    if fn.decorator_list or fn.args.defaults or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg:
        raise ValueError('Unsupported signature')
    if len(fn.args.args) != len(inputs):
        raise ValueError('Changed signature')
    env = dict(zip([a.arg for a in fn.args.args], inputs))
    def visit(node):
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            if not math.isfinite(node.value) or abs(node.value) > 1e9:
                raise ValueError('Literal magnitude limit')
            return node.value
        if isinstance(node, ast.Constant) and type(node.value) in {bool, type(None)}:
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        if isinstance(node, ast.IfExp):
            return visit(node.body if visit(node.test) else node.orelse)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
            return visit(node.values[0]) or visit(node.values[1])
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq): return left == right
            if isinstance(node.ops[0], ast.NotEq): return left != right
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if type(left) not in {int, float} or type(right) not in {int, float}:
                raise ValueError('Only numeric arithmetic')
            if isinstance(node.op, ast.Add): value = left + right
            elif isinstance(node.op, ast.Sub): value = left - right
            elif isinstance(node.op, ast.Mult): value = left * right
            elif isinstance(node.op, ast.Div): value = left / right
            else: raise ValueError('Unsupported arithmetic')
            if not math.isfinite(value) or abs(value) > 1e12:
                raise ValueError('Arithmetic magnitude limit')
            return value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
            value = visit(node.args[0])
            if type(value) is list and len(value) <= 20 and all(type(v) in {int, float} for v in value):
                if node.func.id == 'sum': return sum(value)
                if node.func.id == 'len': return len(value)
        raise ValueError('Unsupported expression: ' + type(node).__name__)
    def statements(body):
        for node in body:
            if isinstance(node, ast.Return): return True, visit(node.value)
            if isinstance(node, ast.If):
                returned, value = statements(node.body if visit(node.test) else node.orelse)
                if returned: return True, value
            else:
                raise ValueError('Unsupported statement: ' + type(node).__name__)
        return False, None
    return statements(fn.body)[1]


def check_holdout(case, source):
    name, cases = ('percent_off', [([200, 10], 180), ([80, 25], 60), ([5, 0], 5), ([9, 100], 0)]) if case == 'discount_large_fix' else (
        'average', [([[]], 0), ([[3]], 3), ([[2, 4, 9]], 5), ([[-4, 4]], 0)])
    try:
        values = [expression_value(source, name, args) for args, expected in cases]
        return {'verified': True, 'passed': sum(v == expected for v, (_, expected) in zip(values, cases)),
                'total': len(cases), 'values': values}
    except Exception as exc:
        return {'verified': False, 'passed': 0, 'total': len(cases), 'error': str(exc)}
