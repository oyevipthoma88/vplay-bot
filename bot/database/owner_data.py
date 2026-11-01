"""Owner panel state — in-memory (resets on bot restart).

Kept in-memory intentionally: MongoDB is currently over quota.
If you clear Atlas quota later, add persistence here.
"""
_state = {
    "maintenance": False,
    "start_pic": None,
}


def is_maintenance() -> bool:
    return bool(_state["maintenance"])


def set_maintenance(on: bool) -> None:
    _state["maintenance"] = bool(on)


def get_start_pic():
    return _state["start_pic"]


def set_start_pic(file_id) -> None:
    _state["start_pic"] = file_id
