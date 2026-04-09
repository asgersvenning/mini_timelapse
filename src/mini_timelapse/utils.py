import re


def natural_sort_key(s: str):
    """
    Sort key for strings that separates into lexical (text) and numeric (int) parts.
    Example: "Dryas_1_101.JPG" -> ("Dryas_", 1, "_", 101)
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]
