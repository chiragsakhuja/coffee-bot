from coffee_bot.store import MenuStore


def test_roundtrip_and_undo(tmp_path, sample_menu):
    store = MenuStore(tmp_path)
    assert store.load().coffees == []

    store.save(sample_menu)
    assert store.load().coffees == sample_menu.coffees

    changed = store.load()
    changed.coffees.pop()
    store.save(changed)
    assert len(store.load().coffees) == 3

    restored = store.undo()
    assert len(restored.coffees) == 4
    assert len(store.load().coffees) == 4


def test_undo_without_history(tmp_path):
    assert MenuStore(tmp_path).undo() is None


def test_no_temp_files_left(tmp_path, sample_menu):
    MenuStore(tmp_path).save(sample_menu)
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["menu.json"]
