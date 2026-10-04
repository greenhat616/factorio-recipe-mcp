"""Research-gate semantics on a synthetic prototype set and save snapshot."""
import hashlib
import json

from recipe_mcp.database import Database


def test_gate_semantics(tmp_path):
    """Script overrides, OR unlocks, prerequisites, disabled research, unknown/mismatched snapshots."""
    raw = {'recipe': {n:{'name':n,'enabled':False,'ingredients':[],'results':[]} for n in ['enabled-script','disabled-script','locked','alternative']},
           'technology': {'a':{'effects':[{'type':'unlock-recipe','recipe':'disabled-script'}, {'type':'unlock-recipe','recipe':'alternative'}]},
                          'b':{'prerequisites':['a'],'effects':[{'type':'unlock-recipe','recipe':'locked'}, {'type':'unlock-recipe','recipe':'alternative'}]}}}
    p=tmp_path/'raw.json'; p.write_text(json.dumps(raw))
    snapshot={'forces':{'one':{'research_enabled':True,'technologies':{'a':{'researched':True,'enabled':True,'level':1},'b':{'researched':False,'enabled':True,'level':1,'prerequisites':['a']}},
                              'recipes':{n:{'enabled':n in ['enabled-script','alternative']} for n in raw['recipe']}}},
              'provenance':{'prototype_raw_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'recipe_definitions_match':True,'data_stage_checksums_match':True}}
    d=Database(path=p,progress=snapshot)
    assert d.availability('enabled-script')['state']=='unlocked'
    assert d.availability('disabled-script')['state']=='script_disabled'
    assert d.availability('locked')['state']=='locked'
    assert d.availability('alternative')['usable_at_stage'] is True
    assert d.technology('b')['state']=='available_to_research'
    snapshot['forces']['one']['technologies']['a']['researched']=False
    assert d.technology('b')['state']=='locked'
    snapshot['forces']['one']['technologies']['b']['enabled']=False
    assert d.technology('b')['state']=='disabled'
    assert Database(path=p,progress={}).availability('locked')['usable_at_stage'] is None
    snapshot['provenance']['prototype_raw_sha256']='bad'
    assert Database(path=p,progress=snapshot).availability('locked')['state']=='unknown'
