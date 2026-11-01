from __future__ import annotations
import threading
from datetime import datetime
from typing import Any
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
from .adapters import builtin_adapters
from .capture import capture_previews
from .carousel import Carousel
from .core import Collector, DesktopRegistry, HyprlandBackend, SnapshotManager, config, update_config
from .recovery import RecoveryExecutor, RecoveryPlanner
from .session import decide


def run(manager: SnapshotManager, backend: HyprlandBackend, prompt: bool = False) -> int:
    app = Gtk.Application(application_id='org.phoenix.Session')
    def activate(application: Any) -> None:
        window = Gtk.ApplicationWindow(application=application, title='Phoenix — история сессии')
        window.set_default_size(1200, 780)
        window.maximize()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for edge in ('top', 'bottom', 'start', 'end'):
            getattr(root, 'set_margin_' + edge)(12)
        window.set_child(root)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16); root.append(header)
        title = Gtk.Label(label='PHOENIX', xalign=0); title.add_css_class('title-1'); header.append(title)
        title2 = Gtk.Label(label='ИСТОРИЯ РАБОЧИХ СТОЛОВ', xalign=0, hexpand=True); title2.add_css_class('dim-label'); header.append(title2)
        rows = manager.history()
        def names() -> list[str]:
            return [f"#{r['id']}  {datetime.fromtimestamp(r['created']).strftime('%d.%m.%Y %H:%M')}  {('📌 ' if r['pinned'] else '') + (r['name'] or ('авто' if r['automatic'] else 'ручной'))}" for r in rows]
        dropdown = Gtk.DropDown.new_from_strings(names() or ['Снимков нет']); header.append(dropdown)
        tip = Gtk.Label(label=('Выберите снимок для восстановления или пропустите. ' if prompt else '') + 'Рабочие столы выбранного снимка: листайте колёсиком, стрелками или нажимая на соседние экраны.', xalign=0, wrap=True)
        tip.add_css_class('dim-label'); root.append(tip)
        carousel = Carousel(manager); carousel.set_vexpand(True); root.append(carousel)
        details = Gtk.Expander(label='Приложения в выбранном снимке'); root.append(details)
        scroll = Gtk.ScrolledWindow(); scroll.set_min_content_height(100); scroll.set_max_content_height(160); scroll.set_propagate_natural_height(True)
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3); scroll.set_child(checks); details.set_child(scroll)
        checkboxes: list[Any] = []
        pane_checks: dict[int, dict[tuple[int, int], Any]] = {}
        output = Gtk.Label(label='', xalign=0, wrap=True, selectable=True); root.append(output)
        buttons = Gtk.Box(spacing=8); root.append(buttons)
        preview = Gtk.Button(label='План восстановления'); snapshot = Gtk.Button(label='Создать снимок'); restore = Gtk.Button(label='Восстановить'); skip = Gtk.Button(label='Пропустить'); settings = Gtk.Button(label='Настройки'); terminal_archive = Gtk.Button(label='Архив терминала'); pin = Gtk.Button(label='Закрепить'); delete = Gtk.Button(label='Удалить')
        restore.add_css_class('suggested-action')
        for button in (preview, snapshot, restore, skip, pin, delete, terminal_archive, settings): buttons.append(button)
        def current() -> tuple[dict[str, Any], dict[str, Any]]:
            if not rows: raise LookupError('Нет снимков')
            selected = dropdown.get_selected()
            if selected >= len(rows): raise LookupError('Снимок не выбран')
            return manager.get(rows[selected]['id'])
        def refresh(*_args: Any) -> None:
            child = checks.get_first_child()
            while child:
                following = child.get_next_sibling(); checks.remove(child); child = following
            checkboxes.clear(); pane_checks.clear()
            if not rows:
                carousel.reload(); carousel.timeline.items.clear(); carousel.timeline.index = None; carousel.area.queue_draw()
            try:
                row, state = current()
                carousel.select_snapshot(row['id'], state, row['created'])
                for wi, w in enumerate(state['windows']):
                    cb = Gtk.CheckButton(label=f"{w.get('class', '?')}  ·  рабочий стол {w.get('workspace', '?')}")
                    cb.set_active(True); checks.append(cb); checkboxes.append(cb)
                    data = w.get('adapters', {}).get('kitty') if isinstance(w.get('adapters'), dict) else None
                    adapter = builtin_adapters({}).get('kitty')
                    if adapter and data and adapter.validate(data):
                        pane_checks[wi] = {}
                        for ti, tab in enumerate(data['tabs']):
                            for pi, pane in enumerate(tab['panes']):
                                label = f"Вкладка {ti+1}, панель {pi+1} · {pane.get('cwd') or 'каталог неизвестен'}"
                                child = Gtk.CheckButton(label=label); child.set_margin_start(26); child.set_active(True)
                                checks.append(child); pane_checks[wi][(ti,pi)] = child
                details.set_label(f'Приложения в снимке: {len(checkboxes)}')
                pin.set_label('Открепить' if row['pinned'] else 'Закрепить')
                output.set_text(f"Выбран снимок #{row['id']}. Листайте рабочие столы этого снимка.")
            except Exception as exc: output.set_text(str(exc))
        dropdown.connect('notify::selected', refresh); refresh()
        def background(fn: Any, after: Any = None) -> None:
            for b in (preview, snapshot, restore): b.set_sensitive(False)
            def work() -> None:
                try: text = fn()
                except Exception as exc: text = 'Ошибка: ' + str(exc)
                def done() -> bool:
                    if after: after()
                    output.set_text(text)
                    for b in (preview, snapshot, restore): b.set_sensitive(True)
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()
        def show_preview(_button: Any) -> None:
            try: row, state = current()
            except Exception as exc: output.set_text(str(exc)); return
            def work() -> str:
                plan = RecoveryPlanner(DesktopRegistry(), manager).plan(state, backend.query('clients'), row['id'])
                lines = []
                for p in plan:
                    lines.append(f"{p['class']}: {p['action']}")
                    for step in p.get('steps', []):
                        dependency = ', '.join(step['depends_on']) or '—'
                        lines.append(f"  {step['id']}: {step['action']} [{step['status']}] · зависит от {dependency}" + (' · требуется подтверждение; не выполняется' if step['requires_confirmation'] else ''))
                return '\n'.join(lines) or 'Нет окон'
            def ready(text: str) -> None:
                dialog = Gtk.Dialog(title='План восстановления', transient_for=window, modal=True)
                dialog.set_default_size(760, 480); dialog.add_button('Закрыть', Gtk.ResponseType.CLOSE)
                scroller = Gtk.ScrolledWindow(vexpand=True); dialog.get_content_area().append(scroller)
                view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR); scroller.set_child(view)
                view.get_buffer().set_text(text)
                dialog.connect('response', lambda d, _r: d.destroy()); dialog.present()
            # Set output before opening the plan dialog.
            for b in (preview, snapshot, restore): b.set_sensitive(False)
            def worker() -> None:
                try: text = work()
                except Exception as exc: text = 'Ошибка: ' + str(exc)
                def done() -> bool:
                    output.set_text('План восстановления подготовлен'); ready(text)
                    for b in (preview, snapshot, restore): b.set_sensitive(True)
                    return False
                GLib.idle_add(done)
            threading.Thread(target=worker, daemon=True).start()
        def take_snapshot(_button: Any) -> None:
            import time
            window.set_visible(False)
            def work() -> str:
                time.sleep(0.5)
                collector = Collector(backend); state = collector.collect()
                if not state['windows']: raise ValueError('Пустой снимок не сохранён')
                images = capture_previews(backend, config())
                number = manager.save(state, previews=images, archives=collector.archives)
                return f'Создан снимок #{number}, изображений: {len(images)}, архивов: {len(manager.archives(number))}. ' + '; '.join(collector.adapter_warnings + manager.last_archive_warnings)
            def finished() -> None:
                rows[:] = manager.history()
                dropdown.set_model(Gtk.StringList.new(names() or ['Снимков нет']))
                carousel.reload(); dropdown.set_selected(0); refresh(); window.present()
            background(work, finished)
        def do_restore(_button: Any) -> None:
            selected = {i for i, cb in enumerate(checkboxes) if cb.get_active()}
            components = {i: {key for key, cb in panes.items() if cb.get_active()} for i, panes in pane_checks.items()}
            try: row, state = current()
            except Exception as exc: output.set_text(str(exc)); return
            def work() -> str:
                result = RecoveryExecutor(backend, DesktopRegistry(), manager).restore(row['id'], state, selected=selected, components=components)
                decide('restore')
                return '\n'.join(f"{r['class']}: {r['status']} — {r.get('detail', '')}" for r in result) or 'Компоненты не выбраны'
            background(work)
        def do_skip(_button: Any) -> None:
            try: decide('skip'); window.close()
            except Exception as exc: output.set_text(str(exc))
        def update_history(selected_id: int | None = None) -> None:
            rows[:] = manager.history()
            dropdown.set_model(Gtk.StringList.new(names() or ['Снимков нет']))
            carousel.reload()
            selected = next((i for i, row in enumerate(rows) if row['id'] == selected_id), 0)
            dropdown.set_selected(selected); refresh()

        def toggle_pin(_button: Any) -> None:
            try:
                row, _ = current(); manager.set_pinned(row['id'], not row['pinned'])
                update_history(row['id'])
            except Exception as exc: output.set_text('Ошибка: ' + str(exc))

        def remove_snapshot(_button: Any) -> None:
            try: row, _ = current()
            except Exception as exc: output.set_text(str(exc)); return
            dialog = Gtk.MessageDialog(transient_for=window, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE, text=f"Удалить снимок #{row['id']}?")
            dialog.set_property('secondary-text', 'Запись снимка будет удалена. Отдельные кадры могут оставаться в истории и резервных копиях.')
            dialog.add_button('Отмена', Gtk.ResponseType.CANCEL); dialog.add_button('Удалить', Gtk.ResponseType.ACCEPT)
            def response(box: Any, answer: int) -> None:
                box.destroy()
                if answer == Gtk.ResponseType.ACCEPT:
                    try: manager.delete(row['id']); update_history(); output.set_text(f"Снимок #{row['id']} удалён")
                    except Exception as exc: output.set_text('Ошибка: ' + str(exc))
            dialog.connect('response', response); dialog.present()

        def show_settings(_button: Any) -> None:
            cfg = config()
            dialog = Gtk.Dialog(title='Настройки Phoenix', transient_for=window, modal=True)
            dialog.add_button('Отмена', Gtk.ResponseType.CANCEL); dialog.add_button('Сохранить', Gtk.ResponseType.ACCEPT)
            dialog.set_default_size(620, 720)
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            dialog.get_content_area().append(scroller)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); scroller.set_child(box)
            for edge in ('top', 'bottom', 'start', 'end'): getattr(box, 'set_margin_' + edge)(16)
            capture = Gtk.CheckButton(label='Сохранять изображения видимых рабочих столов'); capture.set_active(bool(cfg.get('capture_previews', True))); box.append(capture)
            box.append(Gtk.Label(label='Автоматических снимков в истории (закреплённые сохраняются отдельно)', xalign=0, wrap=True))
            retention = Gtk.SpinButton.new_with_range(1, 500, 1); retention.set_value(int(cfg.get('snapshot_retention', 60))); box.append(retention)
            box.append(Gtk.Label(label='Интервал автоснимков, секунд', xalign=0))
            interval = Gtk.SpinButton.new_with_range(10, 3600, 10); interval.set_value(int(cfg.get('interval_seconds', 60))); box.append(interval)
            box.append(Gtk.Label(label=f"База и WAL: {manager.storage_bytes()/1024/1024:.1f} МБ. Изображения могут содержать личные данные.", xalign=0, wrap=True))
            kitty_enabled = Gtk.CheckButton(label='Собирать подробную структуру Kitty'); kitty_enabled.set_active(bool(cfg.get('kitty_enabled', False))); box.append(kitty_enabled)
            kitty_socket = Gtk.Entry(); kitty_socket.set_placeholder_text('Абсолютный путь к приватному Unix-сокету Kitty'); kitty_socket.set_text(str(cfg.get('kitty_socket', ''))); box.append(kitty_socket)
            scrollback = Gtk.CheckButton(label='Архивировать текст терминала (может содержать пароли и токены)'); scrollback.set_active(bool(cfg.get('kitty_scrollback', False))); box.append(scrollback)
            box.append(Gtk.Label(label='Лимит на панель, МиБ', xalign=0))
            pane_limit = Gtk.SpinButton.new_with_range(1, 128, 1); pane_limit.set_value(int(cfg.get('kitty_scrollback_limit', 32*1024**2))/1024**2); box.append(pane_limit)
            box.append(Gtk.Label(label='Общий лимит архива, МиБ', xalign=0))
            total_limit = Gtk.SpinButton.new_with_range(1, 4096, 1); total_limit.set_value(int(cfg.get('kitty_archive_total_limit', 1024**3))/1024**2); box.append(total_limit)
            availability = builtin_adapters(cfg).diagnostics()
            box.append(Gtk.Label(label='Адаптеры: ' + ', '.join(f'{k}: {v["status"]}' for k,v in availability.items()), xalign=0, wrap=True))
            box.append(Gtk.Label(label='Архивы сохраняются без шифрования и входят в резервные копии.', xalign=0, wrap=True))
            purge = Gtk.Button(label='Удалить архивы терминала…'); box.append(purge)
            def confirm_purge(*_args: Any) -> None:
                confirm = Gtk.MessageDialog(transient_for=dialog, modal=True, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE, text='Удалить все архивы терминала?')
                confirm.set_property('secondary-text', 'Резервные копии и свободные страницы SQLite могут сохранять данные.')
                confirm.add_button('Отмена', Gtk.ResponseType.CANCEL); confirm.add_button('Удалить', Gtk.ResponseType.ACCEPT)
                def answer(box_dialog: Any, result: int) -> None:
                    box_dialog.destroy()
                    if result == Gtk.ResponseType.ACCEPT:
                        try: manager.purge_archives(); output.set_text('Архивы удалены из текущей базы; резервные копии не изменены.')
                        except Exception as exc: output.set_text('Ошибка: ' + str(exc))
                confirm.connect('response', answer); confirm.present()
            purge.connect('clicked', confirm_purge)
            backup = Gtk.Button(label='Создать резервную копию сейчас'); box.append(backup)
            backup.connect('clicked', lambda *_: background(lambda: 'Резервная копия: ' + str(manager.backup())))
            def response(box_dialog: Any, answer: int) -> None:
                if answer == Gtk.ResponseType.ACCEPT:
                    try:
                        update_config({'capture_previews': capture.get_active(), 'snapshot_retention': retention.get_value_as_int(), 'interval_seconds': interval.get_value_as_int(), 'kitty_enabled': kitty_enabled.get_active(), 'kitty_scrollback': scrollback.get_active(), 'kitty_socket': kitty_socket.get_text(), 'kitty_scrollback_limit': pane_limit.get_value_as_int() * 1024**2, 'kitty_archive_total_limit': total_limit.get_value_as_int() * 1024**2})
                        output.set_text('Настройки сохранены. Ранее сохранённые изображения остаются в истории и резервных копиях.')
                    except Exception as exc: output.set_text('Ошибка: ' + str(exc))
                box_dialog.destroy()
            dialog.connect('response', response); dialog.present()
        def show_archive(_button: Any) -> None:
            try: row, _ = current(); entries = manager.archives(row['id'])
            except Exception as exc: output.set_text(str(exc)); return
            if not entries: output.set_text('В этом снимке нет архива терминала.'); return
            dialog = Gtk.Dialog(title='Архив терминала — только просмотр', transient_for=window, modal=True)
            dialog.set_default_size(850, 600); dialog.add_button('Закрыть', Gtk.ResponseType.CLOSE)
            box = dialog.get_content_area()
            selector = Gtk.DropDown.new_from_strings([f"Окно {e['window_index'] + 1}, вкладка {e['tab_index'] + 1}, панель {e['pane_index'] + 1} — {e['bytes']} байт" for e in entries]); box.append(selector)
            box.append(Gtk.Label(label='Исторический текст; это не работающий процесс.', xalign=0))
            scroll = Gtk.ScrolledWindow(vexpand=True); box.append(scroll)
            textview = Gtk.TextView(editable=False, monospace=True); scroll.set_child(textview)
            generation = [0]
            def show(*_args: Any) -> None:
                index = selector.get_selected()
                if index >= len(entries): return
                entry = entries[index]; generation[0] += 1; token = generation[0]
                textview.get_buffer().set_text('Загрузка…')
                def worker() -> None:
                    try:
                        from .text import safe_text
                        raw = manager.archive_text(row['id'], entry['window_index'], entry['tab_index'], entry['pane_index'])
                        text = safe_text(raw[:256 * 1024])
                        if len(raw) > 256 * 1024: text += '\n[Показаны первые 256 КиБ; полный архив доступен через CLI.]'
                    except Exception as exc: text = 'Ошибка чтения архива: ' + str(exc)
                    def done() -> bool:
                        if generation[0] == token: textview.get_buffer().set_text(text)
                        return False
                    GLib.idle_add(done)
                threading.Thread(target=worker, daemon=True).start()
            selector.connect('notify::selected', show); dialog.connect('response', lambda d, _r: d.destroy()); show(); dialog.present()
        preview.connect('clicked', show_preview); snapshot.connect('clicked', take_snapshot); restore.connect('clicked', do_restore); skip.connect('clicked', do_skip); settings.connect('clicked', show_settings); pin.connect('clicked', toggle_pin); delete.connect('clicked', remove_snapshot); terminal_archive.connect('clicked', show_archive)
        window.present()
    app.connect('activate', activate)
    return int(app.run([]))
