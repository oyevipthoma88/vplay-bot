def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def progress_bar(elapsed: int, total: int, width: int = 12) -> str:
    """'▰▰▰▰◉▱▱▱▱▱▱▱' style bar. Unknown length -> live style bar."""
    if not total or total <= 0:
        return "━" * (width // 2) + "◉" + "━" * (width // 2 - 1)
    ratio = max(0.0, min(1.0, elapsed / total))
    pos = min(width - 1, int(ratio * width))
    return "▰" * pos + "◉" + "▱" * (width - pos - 1)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def parse_time_to_seconds(value: str) -> int:
    """Parses '90', '1:30', or '1:02:03' into a plain number of seconds.

    Raises ValueError (which the handlers turn into a friendly reply) for
    anything that isn't a plain, non-negative time.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("Time khali hai. Example: /vseek 90 ya /vseek 1:30")

    if ":" not in value:
        if not value.isdigit():
            raise ValueError("Time samajh nahi aaya. Example: /vseek 90 ya /vseek 1:30")
        return int(value)

    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError("Time samajh nahi aaya. Example: /vseek 1:02:03")

    total = 0
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            raise ValueError("Time samajh nahi aaya. Example: /vseek 90 ya /vseek 1:30")
        total = total * 60 + int(part)
    return total
