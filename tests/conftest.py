import logging
import sys
import types
from pathlib import Path


class Link:
    """Stand-in for ProfitView's base class, which only exists in its hosted runtime."""

    def __init__(self):
        self.signals = []

    def signal(self, src, sym, **kwargs):
        self.signals.append((src, sym, kwargs))


class cron:
    @staticmethod
    def run(**schedule):
        return lambda method: method


profitview = types.ModuleType("profitview")
profitview.Link = Link
profitview.cron = cron
profitview.logger = logging.getLogger("profitview")
sys.modules["profitview"] = profitview

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
