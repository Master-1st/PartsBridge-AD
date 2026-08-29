"""LCSC search and long-term Altium library append workflows."""

__version__ = "0.3.8"
__publisher__ = "foke"

from .client import LCSCClient
from .models import Candidate

__all__ = ["Candidate", "LCSCClient", "__publisher__", "__version__"]
