import ast


class SkillSecurityAnalyzer(ast.NodeVisitor):
    """
    通过 Python AST (抽象语法树) 静态分析 Skill 脚本的危险系数。
    """
    DANGEROUS_IMPORTS = {'os', 'sys', 'subprocess', 'shutil', 'socket', 'requests', 'urllib', 'http'}
    DANGEROUS_CALLS = {'eval', 'exec', 'open', '__import__'}

    def __init__(self):
        self.score = 100
        self.warnings = []

    def analyze(self, code_str: str) -> dict:
        self.score = 100
        self.warnings.clear()

        try:
            tree = ast.parse(code_str)
            self.visit(tree)
        except SyntaxError as e:
            return {"score": 0, "warnings": [f"Syntax Error: {e}"], "level": "Fatal"}

        level = "Safe"
        if self.score < 60:
            level = "High Risk"
        elif self.score < 80:
            level = "Medium Risk"

        return {"score": max(0, self.score), "warnings": self.warnings, "level": level}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split('.')[0] in self.DANGEROUS_IMPORTS:
                self.score -= 20
                self.warnings.append(f"Dangerous import detected: '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split('.')[0] in self.DANGEROUS_IMPORTS:
            self.score -= 20
            self.warnings.append(f"Dangerous from...import detected: '{node.module}'")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id in self.DANGEROUS_CALLS:
                self.score -= 30
                self.warnings.append(f"Dangerous function call detected: '{node.func.id}'")
        self.generic_visit(node)