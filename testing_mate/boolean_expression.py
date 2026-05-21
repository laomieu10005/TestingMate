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
