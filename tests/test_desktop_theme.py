"""Validate the real desktop palette with GTK when its display is available."""

import importlib.util
from pathlib import Path
import unittest


UI = Path(__file__).resolve().parents[1] / 'apps/socket_stream_desktop/ui'
spec = importlib.util.spec_from_file_location('desktop_ui_theme', UI / 'ui_theme.py')
theme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(theme)


class DesktopThemeTest(unittest.TestCase):
    def test_gtk_loads_and_applies_workspace_palette(self):
        try:
            import gi
            gi.require_version('Gtk', '3.0')
            from gi.repository import Gtk
        except (ImportError, ValueError):
            self.skipTest('GTK3 bindings not installed')
        if not Gtk.init_check(None)[0]:
            self.skipTest('GTK display not available')
        window = theme.style(Gtk.Window(), 'ss-app')
        self.addCleanup(window.destroy)
        theme.install_theme(window.get_screen())
        title = theme.style(Gtk.Label(label='Practice'), 'ss-title')
        window.add(title)
        window.show_all()
        color = title.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        self.assertAlmostEqual(color.red, 250 / 255, places=2)
        self.assertAlmostEqual(color.blue, 1, places=2)
