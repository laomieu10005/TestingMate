from __future__ import annotations

import re


class BooleanExpressionEvaluator:
    """
    Evaluates boolean expressions using a recursive descent parser.
    Supports both C-style (&&, ||, !) and Python-style (and, or, not) operators.
    Secure: No eval() used, fully parsed AST evaluation.
    """

    def __init__(self):
        self._expression_cache: dict[str, list[str | bool]] = {}

    def combine(self, condition_text: str, decisions: list[str], values: list[bool]) -> bool:
        """
        Evaluate a condition with given decision values.

        Args:
            condition_text: The condition expression (e.g., "D1 && D2 || D3")
            decisions: List of decision variable names (e.g., ["D1", "D2", "D3"])
            values: Corresponding boolean values for each decision

        Returns:
            The evaluated boolean result
        """
        # Create substitution map: decision -> boolean value
        # Empty decisions list is allowed (for expressions with only literals)
        substitutions = {decisions[i]: values[i] for i in range(len(decisions))}

        try:
            # Tokenize and parse
            tokens = self._tokenize(condition_text)
            ast = self._parse_or_expression(tokens, 0, substitutions)
            if ast is None:
                return self._fallback(condition_text, values) if decisions else False
            result, _ = ast
            return result
        except Exception:
            return self._fallback(condition_text, values) if decisions else False

    def _tokenize(self, expression: str) -> list[str]:
        """
        Tokenize a boolean expression into atoms (operators, identifiers, parentheses).

        Handles: &&, ||, !, (, ), identifiers, and case-insensitive true/false literals.
        """
        tokens = []
        i = 0
        while i < len(expression):
            # Skip whitespace
            if expression[i].isspace():
                i += 1
                continue

            # Two-character operators
            if i + 1 < len(expression) and expression[i : i + 2] in ("&&", "||", "!="):
                tokens.append(expression[i : i + 2])
                i += 2
                continue

            # Single-character operators and parentheses
            if expression[i] in "!()":
                tokens.append(expression[i])
                i += 1
                continue

            # Identifiers and keywords (D1, D2, true, false, and, or, not)
            if expression[i].isalnum() or expression[i] == "_":
                j = i
                while j < len(expression) and (expression[j].isalnum() or expression[j] == "_"):
                    j += 1
                tokens.append(expression[i:j])
                i = j
                continue

            # Invalid character
            raise ValueError(f"Invalid character in expression: {expression[i]}")

        return tokens

    def _parse_or_expression(
        self, tokens: list[str], pos: int, substitutions: dict[str, bool]
    ) -> tuple[bool, int] | None:
        """Parse OR expression (lowest precedence)."""
        left = self._parse_and_expression(tokens, pos, substitutions)
        if left is None:
            return None

        result, pos = left
        while pos < len(tokens) and tokens[pos] == "||":
            pos += 1
            right = self._parse_and_expression(tokens, pos, substitutions)
            if right is None:
                return None
            right_val, pos = right
            result = result or right_val

        return result, pos

    def _parse_and_expression(
        self, tokens: list[str], pos: int, substitutions: dict[str, bool]
    ) -> tuple[bool, int] | None:
        """Parse AND expression (medium precedence)."""
        left = self._parse_not_expression(tokens, pos, substitutions)
        if left is None:
            return None

        result, pos = left
        while pos < len(tokens) and tokens[pos] == "&&":
            pos += 1
            right = self._parse_not_expression(tokens, pos, substitutions)
            if right is None:
                return None
            right_val, pos = right
            result = result and right_val

        return result, pos

    def _parse_not_expression(
        self, tokens: list[str], pos: int, substitutions: dict[str, bool]
    ) -> tuple[bool, int] | None:
        """Parse NOT expression (highest precedence unary operator)."""
        if pos >= len(tokens):
            return None

        # Handle unary NOT operator (both ! and 'not')
        negate = False
        while pos < len(tokens) and tokens[pos] in ("!", "not"):
            negate = not negate
            pos += 1

        # Parse the primary expression
        primary = self._parse_primary(tokens, pos, substitutions)
        if primary is None:
            return None

        result, pos = primary
        return (not result) if negate else result, pos

    def _parse_primary(
        self, tokens: list[str], pos: int, substitutions: dict[str, bool]
    ) -> tuple[bool, int] | None:
        """Parse primary expression (literal, identifier, or parenthesized expression)."""
        if pos >= len(tokens):
            return None

        token = tokens[pos]

        # Parenthesized expression
        if token == "(":
            pos += 1
            result = self._parse_or_expression(tokens, pos, substitutions)
            if result is None:
                return None
            result_val, pos = result
            if pos >= len(tokens) or tokens[pos] != ")":
                return None
            return result_val, pos + 1

        # Boolean literals (case-insensitive)
        if token.lower() == "true":
            return True, pos + 1
        if token.lower() == "false":
            return False, pos + 1

        # Identifier/variable lookup
        if token.replace("_", "").isalnum():
            value = substitutions.get(token)
            if value is not None:
                return value, pos + 1
            # Unknown identifier defaults to False
            return False, pos + 1

        return None

    @staticmethod
    def _fallback(condition_text: str, values: list[bool]) -> bool:
        """Fallback heuristic when parsing fails."""
        if "&&" in condition_text and "||" not in condition_text:
            return all(values)
        if "||" in condition_text and "&&" not in condition_text:
            return any(values)
        return values[0] if values else False


class ConditionEvaluator:
    def __init__(self):
        self._evaluator = BooleanExpressionEvaluator()

    def evaluate(self, condition: str, symbol_values: dict[str, str]) -> bool | None:
        """Evaluate a C condition expression given symbol values.
        Returns True/False if deterministic, None if undecidable.
        """
        normalized = self._normalize_condition(condition)
        atoms = self._extract_comparison_atoms(normalized)
        if not atoms:
            return self._eval_single_atom(normalized, symbol_values)

        atom_results = []
        atom_names = []
        rebuilt: list[str] = []
        cursor = 0
        for i, atom in enumerate(atoms):
            result = self._eval_comparison(atom[0], symbol_values)
            if result is None:
                return None
            atom_results.append(result)
            atom_names.append(f"D{i}")
            rebuilt.append(normalized[cursor : atom[1]])
            rebuilt.append(f"D{i}")
            cursor = atom[2]
        rebuilt.append(normalized[cursor:])
        expr = "".join(rebuilt)
        for token in sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", expr)), key=len, reverse=True):
            if token in atom_names or token.lower() in {"true", "false", "and", "or", "not"}:
                continue
            result = self._eval_single_atom(token, symbol_values)
            if result is None:
                return None
            placeholder = f"D{len(atom_names)}"
            atom_names.append(placeholder)
            atom_results.append(result)
            expr = re.sub(rf"\b{re.escape(token)}\b", placeholder, expr)

        return self._evaluator.combine(expr, atom_names, atom_results)

    @classmethod
    def atoms(cls, condition: str) -> list[str]:
        """Return atomic comparison expressions without evaluating them."""
        return [item[0] for item in cls()._extract_comparison_atoms(cls._normalize_condition(condition))]

    @staticmethod
    def _normalize_condition(condition: str) -> str:
        clean = str(condition or "").strip()
        clean = re.sub(r"\([A-Za-z_]\w+(?:\s+[A-Za-z_]\w+)*\)\s*", "", clean)
        return clean

    def _extract_comparison_atoms(self, condition: str) -> list[tuple[str, int, int]]:
        """Extract atomic comparisons from a compound condition."""
        pattern = re.compile(
            r"(?:"
            r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*|"
            r"[-+]?\d+(?:\.\d+)?[uUlLfF]*|'[^']*'|\"[^\"]*\"|true|false|TRUE|FALSE|NULL|nullptr"
            r")\s*(?:==|!=|>=|<=|>|<)\s*(?:"
            r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*|"
            r"[-+]?\d+(?:\.\d+)?[uUlLfF]*|'[^']*'|\"[^\"]*\"|true|false|TRUE|FALSE|NULL|nullptr"
            r")"
        )
        return [(match.group(0), match.start(), match.end()) for match in pattern.finditer(condition)]

    @classmethod
    def _eval_comparison(cls, atom: str, values: dict[str, str]) -> bool | None:
        operand = (
            r"(?:[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*|"
            r"[-+]?\d+(?:\.\d+)?[uUlLfF]*|'[^']*'|\"[^\"]*\"|true|false|TRUE|FALSE|NULL|nullptr)"
        )
        match = re.fullmatch(rf"(?P<left>{operand})\s*(?P<op>==|!=|<=|>=|<|>)\s*(?P<right>{operand})", atom.strip())
        if not match:
            return None
        left_raw = cls._resolve_operand(match.group("left"), values)
        right_raw = cls._resolve_operand(match.group("right"), values)
        if left_raw is None or right_raw is None:
            return None
        return cls._compare(left_raw, right_raw, match.group("op"))

    @classmethod
    def _eval_single_atom(cls, atom: str, values: dict[str, str]) -> bool | None:
        clean = str(atom or "").strip()
        negated = False
        while clean.startswith("!"):
            negated = not negated
            clean = clean[1:].strip()
            if clean.startswith("(") and clean.endswith(")"):
                clean = clean[1:-1].strip()
        raw = cls._resolve_operand(clean, values)
        if raw is None:
            return None
        value = cls._literal(raw)
        result = bool(value) if isinstance(value, (int, float)) else str(raw).strip() not in {"", "0", "false", "FALSE", "NULL", "nullptr"}
        return not result if negated else result

    @classmethod
    def _resolve_operand(cls, raw: str, values: dict[str, str]) -> str | None:
        operand = str(raw or "").strip().strip("()")
        literal = cls._literal(operand)
        if literal is not None or cls._is_string_literal(operand):
            return operand
        key = re.sub(r"\s*(?:->|\.)\s*", ".", operand)
        if key in values:
            return str(values[key])
        if operand in values:
            return str(values[operand])
        suffix = "." + key.split(".", 1)[1] if "." in key else ""
        if suffix:
            for existing, value in values.items():
                if existing.endswith(suffix):
                    return str(value)
        return None

    @classmethod
    def _compare(cls, left_raw: str, right_raw: str, op: str) -> bool | None:
        left = cls._literal(left_raw)
        right = cls._literal(right_raw)
        if left is None or right is None:
            if op == "==":
                return str(left_raw).strip() == str(right_raw).strip()
            if op == "!=":
                return str(left_raw).strip() != str(right_raw).strip()
            return None
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
        return None

    @staticmethod
    def _literal(raw: str) -> float | str | None:
        value = str(raw or "").strip().strip("()")
        if value.lower() == "true":
            return 1.0
        if value.lower() in {"false", "null", "nullptr"}:
            return 0.0
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            return float(ord(value[1])) if len(value) >= 3 else 0.0
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
        value = re.sub(r"[uUlLfF]+$", "", value)
        try:
            return float(int(value, 0))
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None

    @staticmethod
    def _is_string_literal(raw: str) -> bool:
        value = str(raw or "").strip()
        return len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]
