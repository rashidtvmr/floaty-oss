"""Native, inert Q/A timeline for the floating panel.

Every retained pair is one card: the answer on the left, the question on the
right. Provider Markdown is rendered with native GTK widgets and text tags only
— there is no HTML renderer, no network load and no code execution. Model text
is never interpreted as Pango markup or as a URI, so untrusted content stays
inert and selectable.
"""
from answer_format import answer_parts, markdown_blocks, markdown_runs, question_title

# Per-card text tags. The names mirror the legacy answer buffer so theme code
# and tests can keep one vocabulary.
_PROSE_TAGS = ('body', 'strong', 'heading', 'emphasis', 'code', 'muted')

# Bound the rich widgets one card may build. A pathological stream of tiny
# blocks or a huge table must not create unbounded widgets across the retained
# feed; past the budget the answer falls back to one inert, selectable plain
# view that still preserves every character (nothing is silently dropped).
RICH_WIDGET_BUDGET = 150


def _block_cost(block):
    """Rough widget count a block would build, for the rich-budget check."""
    if block['type'] == 'table':
        rows = block['rows']
        return max(1, len(rows) * max((len(row) for row in rows), default=1))
    return 1


def styled(widget, *names):
    for name in names:
        widget.get_style_context().add_class(name)
    return widget


class ConversationReader:
    """Scrollable timeline of retained question/answer pairs."""

    def __init__(self, scroll, on_pin, on_unpin, motion, on_press=None):
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gdk, Gtk, GLib, Pango
        self.Gdk, self.Gtk, self.GLib, self.Pango = Gdk, Gtk, GLib, Pango
        self.scroll, self.on_pin, self.on_unpin, self.motion = scroll, on_pin, on_unpin, motion
        # ``on_press`` is the panel's selectable-text hook. Every selectable
        # view in the feed asks the nonactivating panel to accept keyboard
        # focus on click, so copy/selection works without stealing focus
        # permanently. It is optional so the reader stays testable in isolation.
        self.on_press = on_press
        # All mutable state is initialized before any widget exists or any
        # signal is connected. GTK can deliver a ``destroy`` synchronously while
        # the scrolled window is being rebuilt during teardown, so every handler
        # must be safe to run against a fully-initialized reader.
        self.cards = {}
        self.pinned_id = ''
        self.session_id = ''
        self._jump_source = None
        self._destroyed = False
        # Follow-the-bottom: while the reader sits at the end of the feed, new
        # content keeps it pinned there even as a card's reveal animation grows
        # the scrolled window. Scrolling up releases the pin until the reader
        # returns to the bottom.
        self._stick_bottom = False
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.content.set_border_width(6)
        self.content.get_accessible().set_name('Conversation · question followed by answer')
        child = scroll.get_child()
        if child is not None:
            scroll.remove(child)
        scroll.add(self.content)
        adjustment = scroll.get_vadjustment()
        adjustment.connect('changed', self._on_adjustment_changed)
        adjustment.connect('value-changed', self._on_scroll_value_changed)
        self.content.connect('destroy', self._destroy)

    # ---- lifecycle ------------------------------------------------------

    def _destroy(self, *_):
        self._destroyed = True
        self._cancel_jump()

    def _cancel_jump(self):
        source = getattr(self, '_jump_source', None)
        if source is not None:
            try:
                self.GLib.source_remove(source)
            except Exception:  # noqa: BLE001 - already fired during teardown
                pass
            self._jump_source = None

    def reset(self, session_id):
        """Drop every card. A new session never shows the previous feed."""
        self._cancel_jump()
        for child in self.content.get_children():
            child.destroy()
        self.cards.clear()
        self.session_id = session_id
        # A fresh session starts at the top; release any follow-the-bottom pin
        # so the first card does not drag the reader down as it reveals.
        self._stick_bottom = False

    def _on_scroll_value_changed(self, adjustment):
        if self._destroyed:
            return
        maximum = max(0, adjustment.get_upper() - adjustment.get_page_size())
        self._stick_bottom = adjustment.get_value() >= maximum - 2

    def _on_adjustment_changed(self, adjustment):
        """Keep the reader pinned to the newest content while it grows.

        A newly revealed card animates its height over ~160 ms, so the
        adjustment's upper bound keeps changing after a single snap. Each
        bound change re-snaps while the reader is following the bottom.
        """
        if self._destroyed or not self._stick_bottom:
            return
        maximum = max(0, adjustment.get_upper() - adjustment.get_page_size())
        if adjustment.get_value() != maximum:
            adjustment.set_value(maximum)

    def card_count(self):
        return len(self.cards)

    def _text_views(self):
        """Every TextView in the feed, for tag re-theming."""
        views, pending = [], [self.content]
        while pending:
            widget = pending.pop()
            if isinstance(widget, self.Gtk.TextView):
                views.append(widget)
            if isinstance(widget, self.Gtk.Container):
                pending.extend(widget.get_children())
        return views

    def apply_theme(self, light):
        """Re-color the per-view text tags for the active light/dark theme."""
        foreground = '#202735' if light else '#edf0f4'
        heading = '#245da4' if light else '#cbe3ff'
        code_fg = '#214e86' if light else '#d0e6ff'
        code_bg = '#e0eafa' if light else '#263348'
        muted = '#53647a' if light else '#abb9c9'
        for view in self._text_views():
            table = view.get_buffer().get_tag_table()
            for name, color in {'strong': foreground, 'heading': heading,
                                'code': code_fg, 'muted': muted}.items():
                tag = table.lookup(name)
                if tag is not None:
                    tag.set_property('foreground', color)
            tag = table.lookup('code')
            if tag is not None:
                tag.set_property('background', code_bg)

    # ---- small widget helpers ------------------------------------------

    def selectable(self, widget):
        """Let a click on selectable feed text ask the panel for focus.

        The panel is a nonactivating overlay, so selecting or copying model
        text needs a purposeful click to relax focus; GTK's own handler then
        runs the selection. Returning False keeps that native behaviour.
        """
        if self.on_press is not None:
            widget.add_events(self.Gdk.EventMask.BUTTON_PRESS_MASK)
            widget.connect('button-press-event', self.on_press)
        return widget

    def label(self, text, *classes):
        label = styled(self.Gtk.Label(label=text), *classes)
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(self.Pango.WrapMode.WORD_CHAR)
        label.set_selectable(True)
        label.set_max_width_chars(70)
        return self.selectable(label)

    def prose(self, text, base='body'):
        # Native text tags keep provider Markdown inert, selectable and readable
        # by AT-SPI. Delimiters are never interpreted as Pango/HTML markup.
        # ``base`` lets a table header cell use the native bold tag instead of
        # wrapping the cell in ``**`` delimiters, which would render an empty
        # cell as a literal ``****``.
        view = styled(self.Gtk.TextView(), 'ss-conversation-prose')
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(self.Gtk.WrapMode.WORD_CHAR)
        view.set_hexpand(True)
        view.get_accessible().set_name('Answer text')
        buffer = view.get_buffer()
        for name in _PROSE_TAGS:
            if name == 'strong':
                buffer.create_tag(name, weight=self.Pango.Weight.BOLD)
            elif name == 'heading':
                buffer.create_tag(name, weight=self.Pango.Weight.BOLD, scale=1.12)
            elif name == 'emphasis':
                buffer.create_tag(name, style=self.Pango.Style.ITALIC)
            elif name == 'code':
                buffer.create_tag(name, family='monospace')
            else:
                buffer.create_tag(name)
        for text, tag in markdown_runs(text):
            if tag not in _PROSE_TAGS or tag == 'body':
                tag = base
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), text, tag)
        return self.selectable(view)

    def copy(self, text):
        from gi.repository import Gdk
        clipboard = self.Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)

    # ---- answer rendering ----------------------------------------------

    def _block_widget(self, block):
        """Build one rich block; return ``(widget, text_view_or_None)``."""
        kind = block['type']
        if kind == 'prose':
            view = self.prose(block['text'])
            return view, view
        if kind == 'code':
            return self._code_card(block)
        if kind == 'table':
            return self._table(block), None
        return self._pills(block), None

    def _code_card(self, block):
        Gtk = self.Gtk
        frame = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), 'ss-code-card')
        top = Gtk.Box(spacing=8)
        top.pack_start(self.label(block['language'].upper() or 'CODE', 'ss-eyebrow'), True, True, 0)
        copy = Gtk.Button(label='Copy code')
        copy.get_accessible().set_name('Copy code block')
        copy.connect('clicked', lambda _button, text=block['text']: self.copy(text))
        top.pack_end(copy, False, False, 0)
        frame.pack_start(top, False, False, 0)
        code = styled(Gtk.TextView(), 'ss-conversation-code')
        code.set_editable(False)
        code.set_cursor_visible(False)
        code.set_wrap_mode(Gtk.WrapMode.NONE)
        code.get_accessible().set_name((block['language'] or 'Code') + ' code block')
        code.get_buffer().set_text(block['text'])
        self.selectable(code)
        pane = self._horizontal_pane(code)
        frame.pack_start(pane, False, False, 0)
        return frame, code

    def _horizontal_pane(self, child):
        """A width-bounded scroller so wide code/tables scroll in place.

        ``propagate-natural-width`` off keeps the pane from demanding the
        child's full width, so the feed stays vertical and the wide content
        gets its own horizontal scrollbar instead of clipping.
        """
        Gtk = self.Gtk
        pane = Gtk.ScrolledWindow()
        pane.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        if hasattr(pane, "set_propagate_natural_width"):
            pane.set_propagate_natural_width(False)
        pane.add(child)
        return pane

    def _table(self, block):
        Gtk = self.Gtk
        grid = styled(Gtk.Grid(column_spacing=14, row_spacing=10), 'ss-comparison-table')
        grid.get_accessible().set_name('Comparison table')
        for row, cells in enumerate(block['rows']):
            for col, cell in enumerate(cells):
                # Header cells use the native bold tag, never ``**`` delimiters:
                # an empty header cell must not render as a literal ``****``.
                label = self.prose(cell, 'strong') if row == 0 else self.prose(cell)
                label.set_size_request(100, -1)
                # Distinct, positional names let AT-SPI announce the header and
                # each row/column instead of every cell being "Answer text".
                role = 'Header' if row == 0 else f'Row {row}'
                label.get_accessible().set_name(
                    f'{role}, column {col + 1}: {cell or "empty"}')
                grid.attach(label, col, row, 1, 1)
        return self._horizontal_pane(grid)

    def _pills(self, block):
        Gtk = self.Gtk
        pills = Gtk.FlowBox()
        pills.set_selection_mode(Gtk.SelectionMode.NONE)
        pills.set_max_children_per_line(3)
        pills.set_min_children_per_line(1)
        pills.set_row_spacing(8)
        pills.set_column_spacing(8)
        pills.get_accessible().set_name('Numbered answer highlights')
        for number, item in enumerate(block['items'], 1):
            pill = styled(Gtk.Box(spacing=8), 'ss-answer-pill')
            pill.pack_start(self.label(str(number), 'ss-pill-number'), False, False, 0)
            text = ''.join(part for part, _ in markdown_runs(item))
            label = self.label(text, 'ss-pill-text')
            label.set_max_width_chars(32)
            pill.pack_start(label, True, True, 0)
            pills.add(pill)
        return pills

    # ---- incremental answer sync ---------------------------------------

    def _block_key(self, block):
        """Identity of a block's rendered content, for reuse decisions."""
        kind = block['type']
        if kind == 'prose':
            return (kind, block['text'])
        if kind == 'code':
            return (kind, block['language'], block['text'])
        if kind == 'table':
            return (kind, tuple(tuple(row) for row in block['rows']))
        return (kind, tuple(block['items']))

    def _clear_blocks(self, card):
        for entry in card['answer_blocks']:
            entry['widget'].destroy()
        card['answer_blocks'] = []

    def _append_prose(self, entry, new_text):
        old = entry.get('text', '')
        if not new_text.startswith(old):
            return False
        tail = new_text[len(old):]
        if tail:
            buffer = entry['text_view'].get_buffer()
            for text, tag in markdown_runs(tail):
                buffer.insert_with_tags_by_name(
                    buffer.get_end_iter(), text, tag if tag in _PROSE_TAGS else 'body')
        entry['text'] = new_text
        return True

    def _append_code(self, entry, new_text):
        old = entry.get('text', '')
        if not new_text.startswith(old):
            return False
        tail = new_text[len(old):]
        if tail:
            entry['text_view'].get_buffer().insert(
                entry['text_view'].get_buffer().get_end_iter(), tail)
        entry['text'] = new_text
        return True

    def _update_in_place(self, entry, block):
        """Grow the last block in place so selection and hscroll survive.

        Streaming only ever appends to the final prose/code block. Inserting the
        appended text keeps the GTK selection marks and the code pane's
        horizontal adjustment, which a full rebuild would discard.
        """
        kind = block['type']
        if entry['kind'] != kind:
            return False
        if kind == 'prose':
            return self._append_prose(entry, block['text'])
        if kind == 'code':
            return self._append_code(entry, block['text'])
        return False

    def _sync_blocks(self, card, blocks):
        """Reconcile the answer body with ``blocks``, reusing unchanged widgets."""
        if card['answer_mode'] != 'rich':
            self._clear_blocks(card)
            card['answer_mode'] = 'rich'
        entries = card['answer_blocks']
        if not blocks:
            if not entries:
                placeholder = self.label('Waiting for this answer…', 'ss-muted')
                card['answer_body'].pack_start(placeholder, False, False, 0)
                entries.append({'kind': 'empty', 'key': ('empty',), 'widget': placeholder,
                                'text_view': placeholder, 'text': ''})
            return
        index = 0
        while index < len(entries) and index < len(blocks):
            entry, block = entries[index], blocks[index]
            key = self._block_key(block)
            if entry['key'] == key:
                index += 1
                continue
            if self._update_in_place(entry, block):
                entry['key'] = key
                index += 1
                continue
            break
        for entry in entries[index:]:
            entry['widget'].destroy()
        del entries[index:]
        for block in blocks[index:]:
            widget, text_view = self._block_widget(block)
            card['answer_body'].pack_start(widget, False, False, 0)
            entries.append({'kind': block['type'], 'key': self._block_key(block),
                            'widget': widget, 'text_view': text_view,
                            'text': block.get('text', '')})

    def _render_plain(self, card, markdown):
        """One inert, selectable view when the rich widget budget is exceeded.

        A pathological stream of tiny blocks or a huge table must not create
        unbounded widgets across the retained feed. The plain view keeps every
        character (nothing is dropped) and still grows append-only so selection
        survives.
        """
        if card['answer_mode'] == 'plain' and card['answer_blocks']:
            if self._append_prose(card['answer_blocks'][0], markdown):
                return
        self._clear_blocks(card)
        view = self.prose(markdown or 'Waiting for this answer…')
        card['answer_body'].pack_start(view, False, False, 0)
        card['answer_blocks'] = [{'kind': 'plain', 'key': ('plain', markdown),
                                  'widget': view, 'text_view': view, 'text': markdown}]
        card['answer_mode'] = 'plain'

    # ---- cards ----------------------------------------------------------

    def _card(self, item, pinned):
        Gtk = self.Gtk
        rid = item['request_id']
        reveal = Gtk.Revealer()
        reveal.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        reveal.set_transition_duration(160 if self.motion() else 0)
        row = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), 'ss-conversation-row')
        row.set_valign(Gtk.Align.START)
        reveal.add(row)
        self.content.pack_start(reveal, False, False, 0)
        card = {'widget': reveal, 'row': row, 'answer_box': None,
                'question_box': None, 'question_sig': None, 'answer_sig': None}
        self.cards[rid] = card
        return card

    def _render_question(self, card, item, pinned):
        Gtk = self.Gtk
        rid = item['request_id']
        if card['question_box'] is not None:
            card['question_box'].destroy()
        question = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 'ss-question-card')
        question.set_valign(Gtk.Align.START)
        question.get_accessible().set_name('Question: ' + question_title(
            item['question_display'] or item['question'], limit=120))
        text = item['question_display'] or item['question']
        label = self.label(text)
        question.pack_start(self.label('QUESTION', 'ss-eyebrow'), False, False, 0)
        question.pack_start(label, False, False, 0)
        pin = Gtk.Button(label='Unpin' if pinned else 'Pin question')
        pin.get_accessible().set_name(('Unpin ' if pinned else 'Pin ') + question_title(text, limit=80))
        pin.connect('clicked', lambda _button, key=rid: self.on_unpin() if key == self.pinned_id else self.on_pin(key))
        question.pack_start(pin, False, False, 0)
        card['row'].pack_start(question, False, False, 0)
        card['row'].reorder_child(question, 0)
        card['question_box'] = question
        card['question_sig'] = (text, pinned)

    def _answer_name(self, item):
        """A distinct accessible name, never a bare "Answer: "."""
        title = question_title(item['question_display'] or item['question'], limit=100)
        if not title:
            title = question_title(answer_parts(item['answer'])[0] or item['answer'], limit=100)
        return ('Answer: ' + title) if title else 'Answer'

    def _render_answer(self, card, item):
        Gtk = self.Gtk
        if card['answer_box'] is None:
            answer = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 'ss-answer-card')
            answer.set_hexpand(True)
            eyebrow = self.label('ANSWER', 'ss-eyebrow')
            answer.pack_start(eyebrow, False, False, 0)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            answer.pack_start(body, True, True, 0)
            card['row'].pack_start(answer, True, True, 0)
            card['answer_box'] = answer
            card['answer_eyebrow'] = eyebrow
            card['answer_body'] = body
            card['answer_blocks'] = []
            card['answer_mode'] = None
        status = item['status']
        card['answer_eyebrow'].set_text('ANSWER' if status == 'ready' else status.upper())
        card['answer_box'].get_accessible().set_name(self._answer_name(item))
        _, markdown = answer_parts(item['answer'])
        blocks = markdown_blocks(markdown)
        if sum(_block_cost(block) for block in blocks) > RICH_WIDGET_BUDGET:
            self._render_plain(card, markdown)
        else:
            self._sync_blocks(card, blocks)
        card['answer_sig'] = (item['answer'], status)

    def update(self, history, pinned_id='', session_id=''):
        if self._destroyed:
            return
        if self.session_id != session_id:
            self.reset(session_id)
        self.pinned_id = pinned_id
        wanted = {item['request_id'] for item in history}
        for rid in list(self.cards):
            if rid not in wanted:
                self.cards.pop(rid)['widget'].destroy()
        for item in history:
            rid = item['request_id']
            pinned = rid == pinned_id
            card = self.cards.get(rid)
            changed = card is None
            if card is None:
                card = self._card(item, pinned)
            if card['answer_box'] is None or card['answer_sig'] != (item['answer'], item['status']):
                self._render_answer(card, item)
                changed = True
            if card['question_box'] is None or card['question_sig'] != (
                    item['question_display'] or item['question'], pinned):
                self._render_question(card, item, pinned)
                changed = True
            row = card['row']
            row.get_style_context().remove_class('ss-pinned-row')
            if pinned:
                row.get_style_context().add_class('ss-pinned-row')
            # Only new or changed cards are re-shown; an unchanged card keeps
            # its realized widget tree (and any live selection) untouched.
            if changed:
                card['widget'].show_all()
                card['widget'].set_reveal_child(True)

    # ---- jump -----------------------------------------------------------

    def jump(self, rid):
        """Scroll the pinned card into view; respects reduced-motion."""
        card = self.cards.get(rid)
        if not card or self._destroyed:
            return False
        self._cancel_jump()
        coords = card['widget'].translate_coordinates(self.content, 0, 0)
        if coords is None:
            return False
        adjustment = self.scroll.get_vadjustment()
        target = min(max(0, coords[1]), max(0, adjustment.get_upper() - adjustment.get_page_size()))
        if not self.motion():
            adjustment.set_value(target)
            return True
        start, value = self.GLib.get_monotonic_time(), adjustment.get_value()

        def tick():
            if self._destroyed:
                return False
            progress = min(1, (self.GLib.get_monotonic_time() - start) / 180000)
            adjustment.set_value(value + (target - value) * (1 - (1 - progress) ** 3))
            if progress >= 1:
                self._jump_source = None
                return False
            return True

        self._jump_source = self.GLib.timeout_add(16, tick)
        return True
