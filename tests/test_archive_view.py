import pytest
from phoenix.archive_view import preview, search_archive
from phoenix.core import SnapshotManager


def test_search_entire_archive_and_cross_chunk_unicode(tmp_path):
    manager = SnapshotManager(tmp_path / 'db')
    # Split a multibyte character and the target across SQLite's block boundary.
    content = b'a' * (262144 - 2) + 'Привет-терминал'.encode() + b' tail'
    snap = manager.save({'windows': []}, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': content}])
    chunks = lambda: manager.iter_archive_chunks(snap, 0, 0, 0)
    head = preview(chunks(), 100)
    assert head.text == 'a' * 100 and head.total > 262144
    found = search_archive(chunks(), 'Привет-терминал')
    assert found.match == 262142
    assert found.text[found.selection:found.selection + len('Привет-терминал')] == 'Привет-терминал'
    assert search_archive(chunks(), 'Привет-терминал', found.match + 1).match is None


def test_search_filters_controls_across_boundaries():
    chunks = [b'before\x1b]', b'52;c;hidden\x1b', b'\\after \x1b[', b'31mred\x1b[0m']
    assert preview(chunks).text == 'beforeafter red'
    assert search_archive(chunks, 'hidden').match is None
    assert search_archive(chunks, 'beforeafter').match == 0


def test_search_results_require_integrity_of_whole_archive(tmp_path):
    manager = SnapshotManager(tmp_path / 'db')
    content = b'needle' + b'x' * 300000
    snap = manager.save({'windows': []}, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': content}])
    with manager.connect() as db:
        db.execute('UPDATE archive_chunks SET data=? WHERE digest=(SELECT digest FROM archive_refs WHERE snapshot_id=? AND part=1)', (b'corrupt', snap))
    with pytest.raises(ValueError):
        search_archive(manager.iter_archive_chunks(snap, 0, 0, 0), 'needle')
    with pytest.raises(ValueError):
        preview(manager.iter_archive_chunks(snap, 0, 0, 0))


def test_search_is_bounded_and_can_find_later_occurrences():
    text = b'abc xxx abc'
    assert search_archive([text], 'abc').match == 0
    assert search_archive([text], 'abc', 1).match == 8
    with pytest.raises(ValueError): search_archive([text], 'x' * 1025)


def test_stream_reads_consistent_archive_during_purge(tmp_path):
    manager = SnapshotManager(tmp_path / 'db')
    content = b'A' * 262144 + b'B' * 1000
    snap = manager.save({'windows': []}, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': content}])
    chunks = manager.iter_archive_chunks(snap, 0, 0, 0)
    first = next(chunks)
    manager.purge_archives()
    assert first + b''.join(chunks) == content
    with pytest.raises(LookupError): manager.archive_text(snap, 0, 0, 0)
