"""Rotate the scrape credential without exposing it in YAML or partial writes."""

import os
from pathlib import Path
import tempfile


def write_metrics_token(path, token):
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ValueError("Expected a nonempty bearer token without whitespace")
    path = Path(path)
    # The deployment provisions this private shared directory for UID 1000.
    # Replacing within the directory is atomic for concurrent Prometheus reads.
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
