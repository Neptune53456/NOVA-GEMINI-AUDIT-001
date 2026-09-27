"""Conservative static proofs for explicit, literal keyword objectives.

Unknown objectives are never declared satisfied merely because a token exists.
Only an unconditionally returned call in the explicitly named method is proved.
"""
import ast
import re


def literal_keyword_objective_satisfied(task: str, source: str) -> bool:
    requirements = re.findall(r"\b([A-Za-z_]\w*)\s*=\s*([\"'])([^\"'\n]*)\2", task)
    symbols = re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", task)
    if len(requirements) != 1 or not symbols:
        return False
    keyword, _, expected = requirements[0]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    symbols = list(dict.fromkeys((owner, method) for owner, method in symbols
                   if any(isinstance(node, ast.ClassDef) and node.name == owner for node in tree.body)))
    if len(symbols) != 1:
        return False
    owner_name, method_name = symbols[0]
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner_name]
    if len(owners) != 1:
        return False
    methods = [node for node in owners[0].body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name]
    if len(methods) != 1:
        return False
    method = methods[0]
    returns = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
    if len(returns) != 1 or not method.body or method.body[-1] is not returns[0]:
        return False
    call = returns[0].value
    return isinstance(call, ast.Call) and any(
        item.arg == keyword and isinstance(item.value, ast.Constant) and item.value.value == expected
        for item in call.keywords
    )
