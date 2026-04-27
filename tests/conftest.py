import sys
from unittest.mock import MagicMock

# Mock pyrekordbox before any test imports the rekordbox package
sys.modules["pyrekordbox"] = MagicMock()
sys.modules["pyrekordbox.db6"] = MagicMock()
sys.modules["pyrekordbox.db6.tables"] = MagicMock()
