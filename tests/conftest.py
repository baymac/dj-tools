import sys
from unittest.mock import MagicMock

# Mock pyrekordbox and submodules before any test imports import_to_rekordbox
sys.modules["pyrekordbox"] = MagicMock()
sys.modules["pyrekordbox.db6"] = MagicMock()
sys.modules["pyrekordbox.db6.tables"] = MagicMock()
