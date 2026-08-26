"""LCSC search and long-term Altium library append workflows."""

__version__ = "0.3.1"

from .client import LCSCClient
from .models import Candidate

__all__ = ["Candidate", "LCSCClient", "__version__"]
