def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def parse_time_to_seconds(value: str) -> int:
    """Parses '90', '1:30', or '1:02:03' into a plain number of seconds.

    Raises ValueError (which the handlers turn into a friendly reply) for
    anything that isn't a plain, non-negative time.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("Empty time value")

    if ":" not in value:
        seconds = int(value)
        if seconds < 0:
            raise ValueError("Negative time")
        return seconds

    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError("Too many time components")

    total = 0
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            raise ValueError(f"Invalid time component: {part!r}")
        total = total * 60 + int(part)
    return total
