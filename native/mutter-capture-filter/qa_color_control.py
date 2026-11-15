#!/usr/bin/env python3
"""Positive control: draw the same three bands without exclusion WM_CLASS."""
import gi
gi.require_version('Gtk','3.0')
gi.require_version('Gdk','3.0')
from gi.repository import Gtk, Gdk
Gdk.set_program_class('socket_stream_qa-control')
window=Gtk.Window(title='Capture QA non-excluded color control')
window.set_default_size(400,160)
window.move(80,80)
area=Gtk.DrawingArea()
area.set_size_request(350,120)
def draw(widget, cr):
 for band,color in enumerate(((237,10,169),(9,203,232),(246,215,43))):
  cr.set_source_rgb(*(component/255 for component in color))
  cr.rectangle(0,band*40,widget.get_allocated_width(),40)
  cr.fill()
 return False
area.connect('draw',draw)
window.add(area)
window.connect('destroy',Gtk.main_quit)
window.show_all()
Gtk.main()
