from __future__ import annotations

import re


class BooleanExpressionEvaluator:
    def combine(self, condition_text: str, decisions: list[str], values: list[bool]) -> bool:
        if not decisions:
            return False
        expression = condition_text
        replacements: list[tuple[str, str]] = []
        for index, decision in sorted(enumerate(decisions), key=lambda item: len(item[1]), reverse=True):
            token = f"__D{index}__"
            replacements.append((token, "True" if values[index] else "False"))
            expression = expression.replace(decision, token)
        expression = re.sub(r"\btrue\b", "True", expression, flags=re.I)
        expression = re.sub(r"\bfalse\b", "False", expression, flags=re.I)
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        for token, value in replacements:
            expression = expression.replace(token, value)
        if re.search(r"[^A-Za-z0-9_()\s]", expression):
            return self._fallback(condition_text, values)
        try:
            return bool(eval(expression, {"__builtins__": {}}, {}))
        except Exception:
            return self._fallback(condition_text, values)

    @staticmethod
    def _fallback(condition_text: str, values: list[bool]) -> bool:
        if "&&" in condition_text and "||" not in condition_text:
            return all(values)
        if "||" in condition_text and "&&" not in condition_text:
            return any(values)
        return values[0] if values else False
