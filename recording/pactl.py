def short_names(output: str | bytes) -> set[str]:
    """Return exact device names from `pactl list ... short` output."""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")

    names = set()
    for line in output.splitlines():
        columns = line.split("\t")
        if len(columns) >= 2 and columns[1]:
            names.add(columns[1])
    return names
