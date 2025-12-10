# https://stackoverflow.com/questions/62902096/how-to-evaluate-a-user-input-of-a-string-as-a-math-expression-without-using-eval#:~:text=You%20can%20parse%20the%20expression,parse%20.&text=Then%20you%20can%20analyze%20the,the%20body%20of%20the%20expression.&text=to%20decide%20how%20to%20handle%20the%20operands.&text=See%20the%20documentation%20for%20the,how%20the%20expression%20is%20parsed.&text=This%20shows%20clearly%20that%20the,parse%20).
import ast
import operator

# Mapeia operadores AST para funções do módulo operator
operadores_permitidos = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def avaliar_expressao_segura(expressao):
    arvore = ast.parse(expressao, mode='eval')
    return _avaliar_arvore(arvore.body)


def _avaliar_arvore(node):
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.BinOp):
        # Garante que apenas operadores permitidos sejam usados
        op_func = operadores_permitidos.get(type(node.op))
        if op_func is None:
            raise ValueError(
              f"Operador não permitido: {type(node.op).__name__}")
        left = _avaliar_arvore(node.left)
        right = _avaliar_arvore(node.right)
        return op_func(left, right)
    else:
        raise ValueError(
          f"Expressão inválida ou não permitida: {type(node).__name__}")
