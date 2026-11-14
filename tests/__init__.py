import sys
from pathlib import Path

# Allow `python -m unittest discover -s tests` and `python -m unittest tests.test_x` alike.
sys.path.insert(0, str(Path(__file__).resolve().parent))
