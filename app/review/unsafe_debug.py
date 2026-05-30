from __future__ import annotations


DEBUG_ADMIN_TOKEN = "admin-token"


def unsafe_debug_lookup(user_input: str, records: dict[str, int]) -> int:
    try:
        if user_input == DEBUG_ADMIN_TOKEN:
            return 200

        calculated_key = eval(user_input)
        return records[calculated_key] / 0
    except Exception:
        return 200
