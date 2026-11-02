"""GTK screenshot timeline with large center and angled neighboring frames."""
from __future__ import annotations
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
import math
import cairo
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_foreign('cairo')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
from .animation import smoothstep
from .core import SnapshotManager
from .timeline import Timeline, workspace_items


class Carousel(Gtk.Box):
    def __init__(self, manager: SnapshotManager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.manager = manager
        self.snapshot_id: int | None = None
        self.timeline = Timeline([])
        self.position = 0.0
        self.hovered: int | None = None
        self.hover_levels: dict[int, float] = {}
        self.tick_id = 0
        self.last_frame: float | None = None
        self.transition_start: float | None = None
        self.transition_from = 0.0
        self.transition_duration = .24
        self.generation = 0
        self.pending: set[tuple[int, int, str]] = set()
        self.loader_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='phoenix-images')
        self.cache: OrderedDict[tuple[int, int, str], Any] = OrderedDict()
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_content_width(900); self.area.set_content_height(440)
        self.area.set_draw_func(self.draw); self.area.set_focusable(True)
        self.append(self.area)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)
        self.append(nav)
        left = Gtk.Button(label='← Предыдущий стол'); right = Gtk.Button(label='Следующий стол →')
        self.caption = Gtk.Label(label='', xalign=.5, wrap=True)
        self.caption.set_width_chars(50)
        for child in (left, self.caption, right): nav.append(child)
        left.connect('clicked', lambda *_: self.move(-1))
        right.connect('clicked', lambda *_: self.move(1))
        click = Gtk.GestureClick.new(); click.connect('pressed', self.clicked); self.area.add_controller(click)
        keys = Gtk.EventControllerKey.new(); keys.connect('key-pressed', self.keypress); self.area.add_controller(keys)
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.DISCRETE)
        wheel.connect('scroll', self.scrolled); self.area.add_controller(wheel)
        motion = Gtk.EventControllerMotion.new()
        motion.connect('motion', self.motion); motion.connect('leave', self.leave)
        self.area.add_controller(motion)
        self.update_caption()

    def select_snapshot(self, snapshot_id: int, state: dict[str, Any], created: float | None = None, live: list[dict[str, Any]] | None = None) -> None:
        items = workspace_items(snapshot_id, state, self.manager.previews(snapshot_id), self.manager.earlier_workspace_captures(created) if created is not None else [], live)
        if self.snapshot_id != snapshot_id or self.timeline.items != items:
            previous_workspace = self.timeline.current['workspace'] if self.snapshot_id == snapshot_id and self.timeline.current else None
            self.snapshot_id = snapshot_id
            self.timeline = Timeline(items)
            if previous_workspace is not None:
                self.timeline.index = next((i for i, item in enumerate(items) if item['workspace'] == previous_workspace), self.timeline.index)
            self.generation += 1
            self.cache.clear(); self.pending.clear()
            self.position = float(self.timeline.index or 0); self.hovered = None; self.hover_levels.clear()
            self.transition_start = None; self.transition_from = 0.0
            self.preload_neighbors()
        self.update_caption(); self.area.queue_draw()

    def reload(self) -> None:
        self.snapshot_id = None
        self.generation += 1
        self.cache.clear(); self.pending.clear()

    def move(self, amount: int) -> None:
        if self.timeline.step(amount):
            self.transition_from = self.position
            self.transition_start = None
            self.preload_neighbors()
            self.update_caption(); self.animate()

    def animate(self) -> None:
        settings = Gtk.Settings.get_default()
        if settings and not settings.get_property('gtk-enable-animations'):
            self.position = float(self.timeline.index or 0)
            self.transition_start = None
            self.hover_levels = {self.hovered: 1.0} if self.hovered is not None else {}
            self.area.queue_draw()
            return
        if not self.tick_id:
            self.last_frame = None
            self.tick_id = self.area.add_tick_callback(self.tick)
        self.area.queue_draw()

    def tick(self, _widget: Any, clock: Any) -> bool:
        now = clock.get_frame_time() / 1_000_000
        dt = min(.05, max(0.0, now - self.last_frame)) if self.last_frame is not None else .016
        self.last_frame = now
        factor = 1 - math.exp(-dt * 10)
        target = float(self.timeline.index or 0)
        if self.transition_start is None:
            self.transition_start = now
        progress = min(1.0, max(0.0, (now - self.transition_start) / self.transition_duration))
        self.position = self.transition_from + (target - self.transition_from) * smoothstep(progress)
        indices = set(self.hover_levels)
        if self.hovered is not None: indices.add(self.hovered)
        for index in indices:
            desired = 1.0 if index == self.hovered else 0.0
            self.hover_levels[index] = self.hover_levels.get(index, 0.0) + (desired - self.hover_levels.get(index, 0.0)) * factor
            if desired == 0 and self.hover_levels[index] < .005: self.hover_levels.pop(index, None)
        moving = progress < 1 and abs(target - self.position) > .00001 or any(abs(value - (1 if index == self.hovered else 0)) > .005 for index, value in self.hover_levels.items())
        if not moving:
            self.position = target
            self.transition_from = target
            self.transition_start = None
            self.tick_id = 0
            self.last_frame = None
        self.area.queue_draw()
        return moving

    def motion(self, _controller: Any, x: float, _y: float) -> None:
        if self.timeline.index is None: return
        width = self.area.get_width()
        offset = -1 if x < width * .15 else 1 if x > width * .85 else 0
        index = self.timeline.index + offset
        hovered = index if 0 <= index < len(self.timeline.items) else None
        if hovered != self.hovered:
            self.hovered = hovered
            self.animate()

    def leave(self, _controller: Any) -> None:
        if self.hovered is not None:
            self.hovered = None
            self.animate()

    def clicked(self, _gesture: Any, _n: int, x: float, _y: float) -> None:
        width = self.area.get_width()
        if x < width * .18: self.move(-1)
        elif x > width * .82: self.move(1)
        self.area.grab_focus()

    def keypress(self, _controller: Any, key: int, *_args: Any) -> bool:
        if key in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.move(-1 if key == Gdk.KEY_Left else 1); return True
        return False

    def scrolled(self, _controller: Any, dx: float, dy: float) -> bool:
        amount = dx if abs(dx) > abs(dy) else dy
        if amount: self.move(1 if amount > 0 else -1)
        return bool(amount)

    def update_caption(self) -> None:
        item = self.timeline.current
        if not item:
            self.caption.set_text('В этом снимке нет рабочих столов.')
            return
        if item['captured'] is not None:
            detail = ('Новый кадр (после снимка): ' if item.get('is_live') else 'Более ранний кадр: ' if item['is_earlier'] else 'Кадр от: ') + datetime.fromtimestamp(item['captured']).strftime('%d.%m.%Y %H:%M:%S')
        else:
            detail = 'Схема окон — изображение не было сохранено'
        apps = ', '.join(str(w.get('class', '?')) for w in item['windows']) or 'Нет окон'
        color = '#f4bf75' if item['is_earlier'] else ('#85d6b5' if item['has_image'] else '#aeb8ba')
        headline = GLib.markup_escape_text(f"Рабочий стол {item['workspace']}   ·   {(self.timeline.index or 0) + 1} / {len(self.timeline.items)}   ·   {detail}")
        self.caption.set_markup(f'<span foreground="{color}">{headline}</span>\n{GLib.markup_escape_text(apps)}')

    def preload_neighbors(self) -> None:
        index = self.timeline.index
        if index is None:
            return
        for item in self.timeline.items[max(0, index - 2):index + 3]:
            self.pixbuf(item)

    def pixbuf(self, item: dict[str, Any]) -> Any:
        if not item['has_image']:
            return None
        key = (item['snapshot_id'], item['workspace'], item['monitor'])
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if key in self.pending:
            return None
        self.pending.add(key)
        generation = self.generation
        def load() -> Any:
            try:
                if item['capture_id'] is not None:
                    data = self.manager.capture_image(item['capture_id'], item['capture_source'], item['workspace'], item['monitor'])
                else:
                    preview = next(p for p in self.manager.previews(item['snapshot_id']) if p['workspace'] == item['workspace'] and p['monitor'] == item['monitor'])
                    data = preview['data']
                if not data:
                    return None
                loader = GdkPixbuf.PixbufLoader.new(); loader.write(data); loader.close()
                pix = loader.get_pixbuf()
                if pix is None:
                    return None
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pix.get_width(), pix.get_height())
                context = cairo.Context(surface)
                Gdk.cairo_set_source_pixbuf(context, pix, 0, 0)
                context.paint()
                surface.flush()
                return surface
            except Exception:
                return None
        def finish(future: Any) -> None:
            pix = future.result()
            def publish() -> bool:
                if generation != self.generation:
                    return False
                self.pending.discard(key)
                self.cache[key] = pix
                while len(self.cache) > 10:
                    self.cache.popitem(last=False)
                self.area.queue_draw()
                return False
            GLib.idle_add(publish)
        self.loader_pool.submit(load).add_done_callback(finish)
        return None

    @staticmethod
    def frame(ctx: cairo.Context, pix: Any, x: float, y: float, width: float, height: float, angle: float = 0, item: dict[str, Any] | None = None, darkness: float = 0.0, fast: bool = False) -> None:
        ctx.save()
        ctx.translate(x, y)
        ctx.transform(cairo.Matrix(1, angle, 0, 1, 0, 0))
        ctx.set_source_rgba(0, 0, 0, .5); ctx.rectangle(7, 9, width, height); ctx.fill()
        ctx.rectangle(0, 0, width, height); ctx.clip()
        ctx.set_source_rgb(.105, .14, .16); ctx.paint()
        if pix:
            ctx.save()
            iw, ih = pix.get_width(), pix.get_height()
            scale = min(width / iw, height / ih)
            side = min(1.0, abs(angle) / .20)
            sx = scale + (width / iw - scale) * side
            sy = scale + (height / ih - scale) * side
            ctx.translate((width - iw * sx) / 2, (height - ih * sy) / 2)
            ctx.scale(sx, sy)
            ctx.set_source_surface(pix, 0, 0)
            ctx.get_source().set_filter(cairo.FILTER_FAST if fast else cairo.FILTER_BILINEAR)
            ctx.paint()
            ctx.restore()
        elif item:
            windows = item['windows']
            mon = item.get('monitor_geometry') or {}
            try:
                scale = float(mon.get('scale') or 1)
                mx, my = float(mon['x']), float(mon['y'])
                mw, mh = float(mon['width']) / scale, float(mon['height']) / scale
                if mw <= 0 or mh <= 0: raise ValueError('invalid monitor size')
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                mx, my, mw, mh = 0, 0, 0, 0
            for i, win in enumerate(windows):
                try:
                    px, py = win['at']; sw, sh = win['size']
                    if not mw or not mh: raise ValueError('no geometry')
                    bx, by = (float(px) - mx) / mw, (float(py) - my) / mh
                    bw, bh = float(sw) / mw, float(sh) / mh
                    bx, by = min(.95, max(0, bx)), min(.95, max(0, by))
                    bw, bh = min(1 - bx, max(.05, bw)), min(1 - by, max(.05, bh))
                except (KeyError, TypeError, ValueError):
                    bx, by, bw, bh = i / max(1, len(windows)), .08, 1 / max(1, len(windows)), .84
                ctx.rectangle(bx * width + 2, by * height + 2, max(1, bw * width - 4), max(1, bh * height - 4))
                ctx.set_source_rgb(.21, .31, .33); ctx.fill_preserve()
                ctx.set_source_rgb(.38, .53, .52); ctx.set_line_width(1); ctx.stroke()
                if width > 300:
                    ctx.set_source_rgb(.85, .9, .88); ctx.select_font_face('sans'); ctx.set_font_size(13)
                    ctx.move_to(bx * width + 10, by * height + 22)
                    ctx.show_text(str(win.get('class', '?'))[:35])
        if darkness > 0:
            ctx.set_source_rgba(0, 0, 0, min(.7, darkness))
            ctx.rectangle(0, 0, width, height); ctx.fill()
        ctx.restore()

    def draw(self, _area: Any, ctx: cairo.Context, width: int, height: int) -> None:
        ctx.set_source_rgb(.075, .105, .12); ctx.paint()
        if self.timeline.current is None:
            ctx.set_source_rgb(.68, .74, .75); ctx.select_font_face('sans'); ctx.set_font_size(21)
            ctx.move_to(max(20, width * .25), height * .50); ctx.show_text('Нет изображений для просмотра')
            return
        items = self.timeline.items
        center_width = width * .70
        frame_height = min(height * .88, center_width * 9 / 16)
        center_x = (width - center_width) / 2
        y = (height - frame_height) / 2
        side_width, side_height = width * .125, frame_height * .73
        side_y = (height - side_height) / 2
        peak_hover = max(self.hover_levels.values(), default=0.0)
        # Keep the target panel on top throughout the transition. Switching the
        # drawing order at the midpoint makes overlapping screenshots flicker.
        indices = [i for i in range(len(items)) if abs(i - self.position) < 1.65]
        target_index = self.timeline.index
        for i in sorted(indices, key=lambda j: (j != target_index, abs(j - self.position)), reverse=True):
            distance = i - self.position
            amount = min(1.0, abs(distance))
            side_x = width * (.012 if distance < 0 else .863)
            side_base_y = side_y + (-side_width * .10 if distance < 0 else side_width * .10)
            x = center_x + (side_x - center_x) * amount
            fy = y + (side_base_y - y) * amount
            fw = center_width + (side_width - center_width) * amount
            fh = frame_height + (side_height - frame_height) * amount
            if abs(distance) > 1:
                x += (distance - (1 if distance > 0 else -1)) * width * .2
            angle = (.20 if distance < 0 else -.20) * amount
            hover = self.hover_levels.get(i, 0.0)
            grow = 1 + .06 * hover
            x -= fw * (grow - 1) / 2; fy -= fh * (grow - 1) / 2
            fw *= grow; fh *= grow
            darkness = max(0.0, min(.55, .23 * amount + .30 * peak_hover - .45 * hover))
            self.frame(ctx, self.pixbuf(items[i]), x, fy, fw, fh, angle, items[i], darkness, fast=bool(self.tick_id))
