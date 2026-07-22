"""Put pipeline/src on sys.path so the flat modules import as they do under the
`kalinga.py` launcher, and default the channel to the sample so config resolves."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline", "src"))
os.environ.setdefault("KALINGA_CHANNEL", "daily-science")
