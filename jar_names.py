import os
import re
import unicodedata


def normalize_jar_name(filename):
    name = unicodedata.normalize("NFKC", str(filename or "")).replace("\\", "/")
    name = os.path.basename(name).strip()
    return re.sub(r"(?:\s*\(\d+\))+(?=\.jar$)", "", name, flags=re.IGNORECASE)


def normalize_jar_names(names):
    result = []
    seen = set()
    for name in names or []:
        clean_name = normalize_jar_name(name)
        if clean_name and clean_name not in seen:
            result.append(clean_name)
            seen.add(clean_name)
    return result
