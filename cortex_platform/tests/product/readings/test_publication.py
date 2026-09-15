"""Real publisher processes against disposable libraries; no network or providers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cortex_platform.product.readings.files import PublicationConflict, inventory, read_file
from cortex_platform.product.readings.service import ReadingsService
from cortex_platform.product.sources.adoption import encode_engine_ref

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS publication sandbox')


class Store:
    def __init__(self):
        self.sources = []

    def list_sources(self):
        return self.sources

    def get_source(self, source_id):
        return next(s for s in self.sources if s['id'] == source_id)


@pytest.fixture
def library(tmp_path):
    root = tmp_path.resolve() / 'readings' / 'papers'
    corpus = tmp_path.resolve() / 'corpus'
    root.mkdir(parents=True)
    corpus.mkdir()
    store = Store()
    service = ReadingsService(root=root, state=tmp_path.resolve() / 'state', corpus=corpus, store=store)
    return service, store


def add_paper(service, store, name='paper-one', source_id='source-one'):
    paper = service.corpus / name
    paper.mkdir()
    (paper / 'full_text.md').write_text('original generated text')
    (paper / 'notes.md').write_text('source: arxiv:2601.00042\noriginal notes')
    (paper / 'assets').mkdir()
    (paper / 'assets' / 'figure.svg').write_text('<svg/>')
    source = {'id': source_id, 'canonical_id': 'arxiv:2601.00042', 'engine_ref': encode_engine_ref(name), 'import_state': 'imported'}
    store.sources.append(source)
    return paper


def status(service):
    return service.status()['items'][0]


def test_publish_update_fill_and_preserve_notes(library):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    assert status(service)['state'] == 'published', status(service)
    target = service.root / source.name
    assert (target / 'full_text.md').read_text() == 'original generated text'
    (target / 'notes.md').write_text('human notes')
    (source / 'full_text.md').write_text('new generated text')
    (source / 'notes.md').write_text('replacement notes must not publish')
    (source / 'full_text_ch.md').write_text('translation')
    (source / 'more').mkdir()
    (source / 'more' / 'fig.svg').write_text('new figure')
    service.tick()
    assert status(service)['state'] == 'published', status(service)
    assert (target / 'full_text.md').read_text() == 'new generated text'
    assert (target / 'full_text_ch.md').read_text() == 'translation'
    assert (target / 'more' / 'fig.svg').read_text() == 'new figure'
    assert (target / 'notes.md').read_text() == 'human notes'
    assert list(service.state.glob('staging/*/before/full_text.md'))
    service.tick()
    assert status(service)['state'] == 'published'


def test_manual_edits_are_conflicts_not_overwrites(library):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    target = service.root / source.name / 'full_text.md'
    target.write_text('my corrections')
    (source / 'full_text.md').write_text('automatic regeneration')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert target.read_text() == 'my corrections'


def test_old_sources_require_explicit_tracking_and_do_not_gain_ownership(library):
    service, store = library
    source = add_paper(service, store)
    # Simulate sources that existed when publication was first enabled.
    with service.connect() as db:
        db.execute('INSERT INTO baseline VALUES (?)', ('source-one',))
    service.tick()
    assert service.status()['items'] == []
    target = service.root / source.name
    target.mkdir()
    (target / 'notes.md').write_text('https://arxiv.org/abs/2601.00042\nhandwritten')
    (target / 'full_text.md').write_text('original generated text')
    service.retry('source-one')
    service.tick()
    assert status(service)['state'] == 'published', status(service)
    (source / 'full_text.md').write_text('new text')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert (target / 'full_text.md').read_text() == 'original generated text'


def test_same_title_other_identity_is_not_adopted(library):
    service, store = library
    source = add_paper(service, store)
    target = service.root / source.name
    target.mkdir()
    (target / 'notes.md').write_text('source: arxiv:2601.99999')
    (target / 'full_text.md').write_text('other paper')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert (target / 'full_text.md').read_text() == 'other paper'


@pytest.mark.parametrize('suffix', ['', '.'])
def test_related_work_reference_does_not_establish_directory_identity(library, suffix):
    service, store = library
    source = add_paper(service, store)
    target = service.root / source.name
    target.mkdir()
    notes = f'source: arxiv:2601.99999{suffix}\nRelated work: https://arxiv.org/abs/2601.00042\n'
    (target / 'notes.md').write_text(notes)
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert status(service)['failure'] == 'ambiguous_existing_paper_identity'
    assert sorted(p.name for p in target.iterdir()) == ['notes.md']
    assert (target / 'notes.md').read_text() == notes


def test_non_arxiv_host_does_not_establish_directory_identity(library):
    service, store = library
    source = add_paper(service, store)
    target = service.root / source.name
    target.mkdir()
    (target / 'notes.md').write_text('source: https://not-arxiv.org/abs/2601.00042\n')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert sorted(p.name for p in target.iterdir()) == ['notes.md']


def test_repeated_versions_of_the_same_arxiv_identity_are_unambiguous(library):
    service, store = library
    source = add_paper(service, store)
    target = service.root / source.name
    target.mkdir()
    notes = 'source: arxiv:2601.00042\nPDF: https://arxiv.org/pdf/2601.00042v2.pdf\n'
    (target / 'notes.md').write_text(notes)
    service.tick()
    assert status(service)['state'] == 'published'
    assert (target / 'full_text.md').read_text() == 'original generated text'
    assert (target / 'notes.md').read_text() == notes


def test_post_publication_crash_recovers_without_redownload(library, monkeypatch):
    service, store = library
    source = add_paper(service, store)
    original = service._run
    def crash(task):
        assert original(task)['state'] == 'published'
        raise RuntimeError('simulated parent crash after filesystem commit')
    monkeypatch.setattr(service, '_run', crash)
    with pytest.raises(RuntimeError):
        service.tick()
    restored = ReadingsService(root=service.root, state=service.state, corpus=service.corpus, store=store)
    # Recovery uses the durable snapshot/intent even if source bytes changed.
    (source / 'full_text.md').write_text('later internal edit')
    restored.tick()
    assert status(restored)['state'] == 'published', status(restored)
    assert (restored.root / source.name / 'full_text.md').read_text() == 'original generated text'
    restored.tick()
    assert (restored.root / source.name / 'full_text.md').read_text() == 'later internal edit'


def test_interrupted_update_retains_both_versions_and_refuses_ambiguous_recovery(library, monkeypatch):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    (source / 'full_text.md').write_text('replacement generated text')
    def interrupted(task):
        (service.root / source.name / 'full_text.md').write_text('partial')
        raise RuntimeError('interrupted write')
    monkeypatch.setattr(service, '_run', interrupted)
    with pytest.raises(RuntimeError):
        service.tick()
    restored = ReadingsService(root=service.root, state=service.state, corpus=service.corpus, store=store)
    restored.tick()
    assert status(restored)['state'] == 'conflict'
    assert (service.root / source.name / 'full_text.md').read_text() == 'partial'
    assert any(p.read_text() == 'original generated text' for p in service.state.glob('staging/*/before/full_text.md'))
    assert any(p.read_text() == 'replacement generated text' for p in service.state.glob('staging/*/new/full_text.md'))


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_unsafe_source_entries_are_refused(library, kind):
    service, store = library
    source = add_paper(service, store)
    if kind == 'symlink':
        (source / 'evil').symlink_to(source / 'full_text.md')
    elif kind == 'hardlink':
        os.link(source / 'full_text.md', source / 'evil')
    else:
        os.mkfifo(source / 'evil')
    service.tick()
    assert status(service)['state'] in ('failed', 'conflict')
    assert not (service.root / source.name).exists()


def test_root_replacement_is_refused(library):
    service, store = library
    add_paper(service, store)
    service.root.rename(service.root.with_name('original-papers'))
    service.root.mkdir()
    with pytest.raises(ValueError, match='identity changed'):
        service.tick()
    assert not list(service.root.iterdir())


def test_no_delete_profile_blocks_native_child_and_parent_moves(tmp_path):
    from cortex_platform.product.readings.sandbox import publisher_profile
    root = tmp_path.resolve() / 'readings' / 'papers'
    root.mkdir(parents=True)
    stage = tmp_path.resolve() / 'stage'
    stage.mkdir()
    notes = root / 'notes.md'
    notes.write_text('human')
    profile = publisher_profile(root, stage, [root / 'new'], [])
    operations = [
        "p.unlink()", "p.write_text('')", "p.parent.rename(stage/'moved')",
        "p.parent.parent.rename(stage/'moved')", "os.link(p,stage/'alias')",
        "subprocess.run(['/bin/rm',str(p)],check=True)",
        "assert ctypes.CDLL(None).unlink(os.fsencode(p)) == 0",
    ]
    for operation in operations:
        code = f'import os,ctypes,subprocess\nfrom pathlib import Path\np=Path({str(notes)!r})\nstage=Path({str(stage)!r})\n' + operation
        process = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code], capture_output=True)
        assert process.returncode != 0, operation
        assert notes.read_text() == 'human'
    code = f"from pathlib import Path; Path({str(root / 'new')!r}).mkdir(); Path({str(stage / 'temp')!r}).write_text('x'); Path({str(stage / 'temp')!r}).unlink()"
    assert subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code], capture_output=True).returncode == 0


def test_conflict_does_not_prevent_missing_file_completion(library):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    target = service.root / source.name
    (target / 'full_text.md').write_text('human edited body')
    (source / 'full_text.md').write_text('new automatic body')
    (source / 'full_text_ch.md').write_text('new translation')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert (target / 'full_text.md').read_text() == 'human edited body'
    assert (target / 'full_text_ch.md').read_text() == 'new translation'


def test_target_symlink_never_changes_outside_file(library, tmp_path):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    target = service.root / source.name / 'full_text.md'
    target.unlink()
    outside = tmp_path / 'outside'
    outside.write_text('original generated text')
    target.symlink_to(outside)
    (source / 'full_text.md').write_text('regeneration')
    service.tick()
    assert status(service)['state'] == 'failed'
    assert outside.read_text() == 'original generated text'


def test_two_publishers_serialize(library):
    service, store = library
    add_paper(service, store)
    other = ReadingsService(root=service.root, state=service.state, corpus=service.corpus, store=store)
    with service.lock():
        with pytest.raises(BlockingIOError):
            other.tick()
    assert not list(service.root.iterdir())
    other.tick()
    assert status(service)['state'] == 'published'


def test_existing_empty_directory_is_preserved(library):
    service, store = library
    source = add_paper(service, store)
    target = service.root / source.name
    target.mkdir()
    service.tick()
    assert status(service)['state'] != 'published'
    assert list(target.iterdir()) == []


def test_read_only_effect_profile_is_inherited_by_subprocesses(tmp_path):
    from cortex_platform.product.readings.sandbox import read_only_profile
    root = tmp_path.resolve() / 'papers'
    root.mkdir()
    notes = root / 'notes.md'
    notes.write_text('human')
    profile = read_only_profile((root,))
    child = f"from pathlib import Path; Path({str(notes)!r}).write_text('overwrite')"
    code = f"import subprocess,sys; subprocess.run([sys.executable,'-c',{child!r}],check=True)"
    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code], capture_output=True)
    assert result.returncode != 0
    assert notes.read_text() == 'human'


def test_same_content_replaced_paper_directory_is_not_owned(library):
    import shutil
    service, store = library
    source = add_paper(service, store)
    service.tick()
    target = service.root / source.name
    original = service.root / 'moved-by-operator'
    target.rename(original)
    shutil.copytree(original, target)
    (source / 'full_text.md').write_text('new automatic text')
    service.tick()
    assert status(service)['state'] == 'conflict'
    assert (target / 'full_text.md').read_text() == 'original generated text'


def test_source_file_removal_never_propagates_deletion(library):
    service, store = library
    source = add_paper(service, store)
    service.tick()
    (source / 'assets' / 'figure.svg').unlink()
    service.tick()
    assert status(service)['state'] == 'published'
    assert (service.root / source.name / 'assets' / 'figure.svg').read_text() == '<svg/>'


def test_source_pending_at_enablement_is_published_when_adopted(library):
    service, store = library
    source = add_paper(service, store)
    store.sources[0]['import_state'] = 'pending'
    store.sources[0]['engine_ref'] = None
    other = ReadingsService(root=service.root, state=service.state.parent / 'fresh-state', corpus=service.corpus, store=store)
    other.tick()
    assert not other.status()['items']
    store.sources[0]['import_state'] = 'imported'
    store.sources[0]['engine_ref'] = encode_engine_ref(source.name)
    other.tick()
    assert status(other)['state'] == 'published'


def test_separate_installations_share_the_library_lock(library):
    service, store = library
    other = ReadingsService(root=service.root, state=service.state.parent / 'other-state', corpus=service.corpus, store=store)
    with service.lock():
        with pytest.raises(BlockingIOError):
            other.tick()
