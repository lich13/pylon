from pylon.tasks.marketplace import policy,taxonomy
from pylon.tasks.marketplace.classifier import classify_listing


def selection(**fields):
    return {'enabled':True,'version':2,'categories':[],**fields}


def test_blocked_group_overrides_leaf_type_and_whole_group_including_future_child():
    value=selection(groups=['component'],types=['component.memory'],
                    categories=['component.gpu'],blocked_groups=['component'])
    for key in ['component.gpu','component.memory_laptop','component.future_accelerator']:
        assert not policy.selected(key,value)
    assert policy.effective(value)==[]


def test_whole_group_covers_future_children_and_unselected_group_stays_quiet():
    value=selection(groups=['component'])
    assert policy.selected('component.future_accelerator',value)
    assert not policy.selected('camera.body',value)


def test_partial_type_selection_does_not_enable_siblings():
    value=selection(types=['component.memory','camera.lens'])
    assert policy.selected('component.memory_ecc',value)
    assert policy.selected('component.memory_laptop',value)
    assert policy.selected('camera.lens',value)
    assert not policy.selected('camera.body',value)
    assert not policy.selected('component.cpu',value)


def test_blocked_type_overrides_parent_group_for_future_leaves():
    value=selection(groups=['component'],blocked_types=['component.memory'])
    assert not policy.selected('component.memory_ecc',value)
    assert not policy.selected('component.memory_laptop',value)
    assert policy.selected('component.gpu',value)


def test_partial_type_under_parent_group_requires_an_explicit_leaf():
    value=selection(groups=['component'],partial_types=['component.memory'],
                    categories=['component.memory_ecc'])
    assert policy.selected('component.memory_ecc',value)
    assert not policy.selected('component.memory_laptop',value)
    assert policy.selected('component.gpu',value)


def test_mixed_post_only_highlights_nonblocked_available_item():
    items=classify_listing('出售几件闲置',
        'RTX 4070 显卡2000元出售\niPhone 15 3000元出售\n相机1000元已售','chiphell',True)
    hits=policy.evaluate(items,selection(groups=['component','phone','camera'],blocked_groups=['component']))
    assert [item['category'] for item in hits]==['phone.smartphone']


def test_hierarchy_covers_every_leaf_once_and_keeps_brands_out():
    hierarchy=taxonomy.hierarchy()
    leaves=[key for group in hierarchy.values() for kind in group['types'].values() for key in kind['categories']]
    assert len(leaves)==len(set(leaves))==len(taxonomy.CATEGORIES)
    assert {'component','camera'} <= hierarchy.keys()
    assert {'component.gpu','component.cpu','component.mainboard','component.memory'} <= hierarchy['component']['types'].keys()
    assert {'camera.body','camera.lens'} <= hierarchy['camera']['types'].keys()
    assert not {'phone.apple','phone.android','tablet.ipad'} & set(leaves)


def test_legacy_policy_aliases_migrate_and_group_block_still_wins(tmp_path):
    import json
    path=tmp_path/'policy.json'
    path.write_text(json.dumps(selection(categories=['phone.apple'],blocked_groups=['phone'])))
    value=policy.load(path)
    assert value['categories']==['phone.smartphone']
    assert not policy.effective(value)
