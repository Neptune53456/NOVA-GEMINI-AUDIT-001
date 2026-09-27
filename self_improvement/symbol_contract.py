"""Static method compatibility checks on a complete Python candidate."""
import ast


def parameter_contract(arguments):
    return (
        tuple(arg.arg for arg in arguments.posonlyargs),
        tuple(arg.arg for arg in arguments.args),
        arguments.vararg.arg if arguments.vararg else None,
        tuple(arg.arg for arg in arguments.kwonlyargs),
        arguments.kwarg.arg if arguments.kwarg else None,
    )


def _classes(tree):
    result = {}
    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = prefix + node.name
                result[name] = node
                visit(node.body, name + ".")
    visit(tree.body)
    return result


def _methods(owner):
    return {node.name: node for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _receiver_calls(method):
    args = [*method.args.posonlyargs, *method.args.args]
    if not args:
        return set()
    receiver = args[0].arg
    return {node.func.attr for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == receiver}


def validate_method_contracts(original, candidate):
    before = _classes(ast.parse(original))
    after = _classes(ast.parse(candidate))
    for owner_name, owner in before.items():
        original_methods = _methods(owner)
        candidate_owner = after.get(owner_name)
        if candidate_owner is None:
            continue  # Whole-class removal is outside a method replacement.
        candidate_methods = _methods(candidate_owner)
        available = set(candidate_methods)
        # Resolve statically visible local base classes, never import candidate code.
        pending = list(candidate_owner.bases)
        visited = set()
        while pending:
            base = pending.pop()
            if isinstance(base, ast.Name) and base.id in after and base.id not in visited:
                visited.add(base.id)
                available.update(_methods(after[base.id]))
                pending.extend(after[base.id].bases)
        for name, method in original_methods.items():
            replacement = candidate_methods.get(name)
            if replacement is None:
                if any(name in _receiver_calls(item) for item in candidate_methods.values()):
                    raise ValueError(f"removed referenced method: {owner_name}.{name}")
                continue
            if parameter_contract(method.args) != parameter_contract(replacement.args):
                raise ValueError(f"symbol parameter contract changed: {owner_name}.{name}")
            missing = _receiver_calls(replacement) - _receiver_calls(method) - available
            if missing:
                raise ValueError("unresolved new receiver method references: " + ", ".join(sorted(missing)))
