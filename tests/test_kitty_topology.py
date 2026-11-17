from phoenix.adapters.kitty import KittyAdapter
from phoenix.adapters.kitty_topology import derive_plan


def test_small_layout_requires_consistent_directional_neighbors():
    windows = [
        {'id': 91, 'neighbors': {'right': [92, 93]}},
        {'id': 92, 'neighbors': {'left': [91], 'bottom': [93]}},
        {'id': 93, 'neighbors': {'left': [91], 'top': [92]}},
    ]
    plan = derive_plan(windows)
    assert plan is not None and {step['pane'] for step in plan} == {0, 1, 2}
    windows[2]['neighbors']['left'] = [999]
    assert derive_plan(windows) is None
    windows[2]['neighbors'] = {}
    assert derive_plan(windows) is None
    assert derive_plan([{'id': 1}, {'id': 2}]) is None
    assert derive_plan([{'id': 1, 'neighbors': {}}, {'id': 1, 'neighbors': {}}]) is None
    assert derive_plan([{'id': i, 'neighbors': {}} for i in range(1, 5)]) is None


def test_topology_uses_only_validated_instructions_and_partial_selection_falls_back():
    adapter = KittyAdapter({})
    plan = [{'pane': 0, 'parent': None, 'axis': None}, {'pane': 1, 'parent': 0, 'axis': 'vsplit'}]
    data = {'version': 1, 'active_tab': 0, 'tabs': [{'layout': 'splits', 'active_pane': 0, 'topology': plan, 'panes': [{'cwd': '/tmp', 'process': 'unknown'}, {'cwd': '/tmp', 'process': 'unknown'}]}]}
    selected = adapter.select(data, {(0, 0), (0, 1)})
    assert selected['tabs'][0]['topology'] == plan
    session = adapter.session(selected)
    assert '--location=vsplit' in session and 'focus_matching_window' in session
    partial = adapter.select(data, {(0, 1)})
    assert 'topology' not in partial['tabs'][0]
    from pathlib import Path
    from phoenix.recovery import RecoveryPlanner
    class Registry:
        def find(self, _class): return Path('/tmp/kitty.desktop')
    state = {'windows': [{'class': 'kitty', 'adapters': {'kitty': data}}]}
    preview = RecoveryPlanner(Registry()).plan(state, [], components={0: {(0, 1)}})
    tab = next(s for s in preview[0]['steps'] if s['id'] == 'tab:0')
    assert tab['status'] == 'partial' and 'will not be applied' in tab['detail']
    data['tabs'][0]['topology'][1]['axis'] = 'vsplit\nlaunch evil'
    assert not adapter.validate(data)
    data['tabs'][0]['topology'][1]['axis'] = 'vsplit'
    data['tabs'][0]['topology'][1]['parent'] = '0\nlaunch evil'
    assert not adapter.validate(data)
