def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def parse_time_to_seconds(value: str) -> int:
    """Parses '90', '1:30', or '1:02:03' into a plain number of seconds."""
    value = value.strip()
    if ":" not in value:
        return int(value)
    parts = [int(p) for p in value.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds
