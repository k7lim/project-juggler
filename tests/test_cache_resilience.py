from pj import cache


def test_save_tolerates_unwritable_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, 'cache_dir', lambda: tmp_path)
    monkeypatch.setattr(cache, '_signatures', lambda: {})
    def denied(*args, **kwargs):
        raise PermissionError('read-only cache')
    monkeypatch.setattr('builtins.open', denied)
    cache.save([{'name': 'example'}])
