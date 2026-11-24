"""Shared GTK3 visual language for the workspace and floating reading panel."""

CSS = b"""
.ss-app { background-image: linear-gradient(145deg, #30345b, #181b31); color: #f0effb; }
.ss-dialog { background-color: #fafafa; color: #18181b; }
.ss-app headerbar { background: #f4f4f5; border-bottom: 1px solid #e4e5e9; box-shadow: none; }
.ss-app label, .ss-dialog label { color: #24262c; }
.ss-app button, .ss-dialog button, .ss-overlay button {
  background-image: none; background-color: #292e36; color: #e9edf3;
  border: 1px solid #3b424d; border-radius: 9px; box-shadow: none;
  text-shadow: none; padding: 8px 13px; min-height: 18px;
  transition: background-color 120ms ease, border-color 120ms ease;
}
.ss-app button:hover, .ss-dialog button:hover, .ss-overlay button:hover {
  background-color: #363e48; border-color: #596474;
}
.ss-app button:disabled, .ss-dialog button:disabled, .ss-overlay button:disabled {
  color: #74808e; background-color: #20252c; border-color: #303740;
}
.ss-app button:focus, .ss-dialog button:focus, .ss-overlay button:focus {
  border-color: #a5dbcf;
}
.ss-app button.ss-primary, .ss-dialog button.ss-primary, .ss-overlay button.ss-primary {
  background-image: linear-gradient(to bottom, #6395e9, #4883d9); color: #ffffff; border-color: #6096e6; font-weight: 600;
}
.ss-app button.ss-primary label, .ss-dialog button.ss-primary label { color: #ffffff; }
.ss-app button.ss-primary:hover, .ss-dialog button.ss-primary:hover { background-image: linear-gradient(to bottom, #75a5f5, #5793ea); }
.ss-app button.ss-primary:disabled { background-image: none; background-color: #dce5f3; border-color: #dce5f3; color: #849bbd; }
.ss-app button.ss-quiet, .ss-overlay button.ss-quiet { background-color: transparent; border-color: transparent; }
.ss-app button.ss-quiet:hover, .ss-overlay button.ss-quiet:hover { background-color: rgba(150,170,195,0.12); }
.ss-app button.ss-danger, .ss-overlay button.ss-danger { color: #f2b3b8; }
.ss-app entry, .ss-dialog entry, .ss-app spinbutton, .ss-dialog spinbutton {
  color: #18181b; background: #ffffff; border: 1px solid #d9dce2; border-radius: 8px;
  padding: 7px 9px; box-shadow: none;
}
.ss-app entry:focus, .ss-dialog entry:focus { border-color: #699ceb; }
.ss-app entry.ss-recording { border-color: #397dd8; background-color: #edf3fc; }
.ss-app checkbutton, .ss-dialog checkbutton { color: #24262c; }
.ss-app checkbutton check, .ss-dialog checkbutton check { background: #ffffff; border: 1px solid #b7bdc8; }
.ss-app checkbutton check:checked, .ss-dialog checkbutton check:checked { background: #568fe6; color: #ffffff; }
.ss-app separator, .ss-overlay separator { background: rgba(160,175,195,0.15); min-height: 1px; }
.ss-nav { background: #f2f3f5; border-right: 1px solid #e4e5e9; padding: 20px 12px; }
.ss-app .ss-nav button { background: transparent; border: none; padding: 11px 14px; border-radius: 8px; }
.ss-app .ss-nav button label { color: #69707e; }
.ss-app .ss-nav button:checked { background: #e0eaf9; }
.ss-app .ss-nav button:checked label { color: #2762b5; font-weight: 600; }
.ss-card { background: #ffffff; border: 1px solid #e3e5ea; border-radius: 14px; padding: 20px; }
.ss-app .ss-title { font-size: 27px; font-weight: 600; color: #18181b; }
.ss-app .ss-section-title { font-size: 17px; font-weight: 600; color: #24262c; }
.ss-app .ss-eyebrow, .ss-overlay .ss-eyebrow { font-size: 10px; font-weight: 700; letter-spacing: 1.4px; color: #a1adbc; }
.ss-app .ss-muted, .ss-dialog .ss-muted { color: #727b8a; font-size: 12px; }
.ss-overlay .ss-muted { color: #9fa4b1; font-size: 12px; }
.ss-app .ss-small, .ss-overlay .ss-small { font-size: 12px; }
.ss-app .ss-brand { color: #397dd8; font-weight: 700; font-size: 18px; }
.ss-overlay .ss-brand { color: #cbe3ff; font-weight: 700; font-size: 18px; }
.ss-app .ss-chip, .ss-overlay .ss-chip { background: rgba(106,166,238,0.14); color: #78b3f5; border-radius: 12px; padding: 4px 10px; font-size: 11px; }
.ss-app .ss-chip { color: #376cae; }
.ss-app .ss-error, .ss-dialog .ss-error { color: #b44750; }
.ss-overlay .ss-error { color: #f2b3b8; }
.ss-app list, .ss-app list row { background: transparent; color: #24262c; }
.ss-overlay list, .ss-overlay list row { background: transparent; color: #e9edf3; }
.ss-app list row, .ss-overlay list row { border-radius: 8px; padding: 6px 9px; }
.ss-app list row:hover, .ss-overlay list row:hover { background: rgba(150,170,195,0.10); }
.ss-app list row:selected, .ss-overlay list row:selected { background: rgba(106,166,238,0.15); }
.ss-app .ss-diagnostics, .ss-app .ss-diagnostics text { background: #11151b; color: #a8b6c8; font-size: 12px; }
.ss-overlay { background-color: transparent; color: #edf0f4; }
.ss-panel { border-radius: 16px; border: 1px solid rgba(161,167,181,0.55); }
.ss-command-bar { border-radius: 26px; border: 1px solid rgba(177,189,210,0.58); padding: 8px 12px; }
.ss-overlay label { color: #e8edf4; }
.ss-overlay button { padding: 5px 9px; min-height: 16px; border-radius: 7px; font-size: 12px; }
.ss-overlay button.ss-icon { padding: 6px; min-width: 18px; min-height: 18px; }
.ss-overlay button.ss-close { border: 1px solid #7b8190; border-radius: 20px; background: #17171c; padding: 9px; }
.ss-overlay .ss-question { color: #cbe3ff; font-size: 18px; font-weight: 500; }
.ss-overlay button.ss-question-bubble { background-image: linear-gradient(to bottom, rgba(15,76,160,0.90), rgba(4,43,100,0.90)); border: 1px solid #2b64ac; border-radius: 15px 15px 5px 15px; padding: 10px 14px; }
.ss-overlay button.ss-question-bubble:hover { border-color: #6399dc; }
.ss-overlay .ss-footer { border-top: 1px solid rgba(175,193,214,0.15); padding: 9px 14px; }
.ss-overlay .ss-pin:checked { background: rgba(106,166,238,0.18); color: #cbe3ff; border-color: rgba(106,166,238,0.45); }
.ss-overlay .ss-history { background: rgba(10,15,22,0.35); border-top: 1px solid rgba(175,193,214,0.15); }
.ss-overlay .ss-answer, .ss-overlay .ss-answer text { background: transparent; color: #edf0f4; }
.ss-overlay .ss-transcript, .ss-overlay .ss-transcript text { background: transparent; color: #abb9c9; font-size: 13px; }
.ss-overlay .ss-answer text selection { background: #355078; color: #ffffff; }
.ss-overlay scrollbar slider, .ss-app scrollbar slider { background: #596474; min-width: 5px; min-height: 28px; border-radius: 6px; }
.ss-overlay scrollbar, .ss-app scrollbar { background: transparent; }
.ss-overlay scale trough, .ss-app scale trough { background: #394550; min-height: 4px; border-radius: 4px; }
.ss-overlay scale highlight, .ss-app scale highlight { background: #6fa5ed; }
.ss-overlay scale slider, .ss-app scale slider { background: #8bb6f0; border: none; min-width: 12px; min-height: 12px; border-radius: 6px; }
.ss-app button, .ss-dialog button { background-image: none; background-color: #ffffff; color: #3f4652; border-color: #dce0e6; }
.ss-app button:hover, .ss-dialog button:hover { background-color: #f1f5fb; border-color: #b9c9e0; }
.ss-app button:disabled, .ss-dialog button:disabled { color: #929aa7; background-color: #f2f3f5; border-color: #e4e6eb; }
.ss-app button.ss-primary, .ss-dialog button.ss-primary { background-image: linear-gradient(to bottom, #6395e9, #4883d9); border-color: #6096e6; color: #ffffff; }
.ss-app button.ss-danger label { color: #b44750; }
.ss-app .ss-shortcut { font-family: monospace; font-size: 12px; }
.ss-app .ss-banner { background: #edf3fc; border: 1px solid #d4e2f6; border-radius: 10px; padding: 12px 16px; }
.ss-app headerbar { background: #242741; border-color: #45496e; }
.ss-app headerbar label { color: #efeffa; }
.ss-app label, .ss-app checkbutton, .ss-app list, .ss-app list row { color: #efeffa; }
.ss-app .ss-title, .ss-app .ss-section-title { color: #faf9ff; }
.ss-app .ss-muted, .ss-app .ss-eyebrow { color: #bfc3e1; }
.ss-app .ss-nav { background: #20223a; border-color: #45496e; }
.ss-app .ss-nav button label { color: #c4c8e4; }
.ss-app .ss-nav button:checked { background: #444c80; }
.ss-app .ss-nav button:checked label { color: #fff; }
.ss-app .ss-card { background: rgba(126, 135, 192, 0.15); border-color: #50557a; }
.ss-app .ss-banner { background: #292f51; border-color: #50557a; }
.ss-app .ss-chip { color: #b9d7ff; }
.ss-app button, .ss-app entry, .ss-app spinbutton { background: #2e3354; border-color: #535a83; color: #f0effb; }
.ss-app button:hover { background: #3e456d; border-color: #7783ae; }
.ss-app button:disabled { background: #292d48; color: #a1a6c3; }
.ss-app button.ss-primary, .ss-overlay button.ss-primary { background-image: linear-gradient(to bottom, #5469cb, #3f51aa); border-color: #7989e3; }
.ss-app button.ss-primary:disabled { background-image: none; }
.ss-app .ss-error, .ss-app button.ss-danger label { color: #ffb8c1; }
.ss-overlay .ss-question-card, .ss-overlay .ss-answer-card {
  background: rgba(113, 130, 192, 0.13); border: 1px solid rgba(151, 166, 222, 0.34);
  border-radius: 13px; padding: 12px;
}
.ss-overlay .ss-question-card { background: rgba(111, 124, 214, 0.25); }
.ss-overlay .ss-pinned-row .ss-question-card { border-color: #a5b7fd; }
.ss-reduced-motion button { transition: none; }
"""

_providers = []


def style(widget, *classes):
    for name in classes:
        widget.get_style_context().add_class(name)
    return widget


def install_theme(screen):
    from gi.repository import Gtk

    if _providers:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _providers.append(provider)
