"""Regression checks: actual stdio MCP, research gates, and recipe balances."""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from database import Database, ROOT


def check_gate_semantics():
    raw = {'recipe': {n:{'name':n,'enabled':False,'ingredients':[],'results':[]} for n in ['enabled-script','disabled-script','locked','alternative']},
           'technology': {'a':{'effects':[{'type':'unlock-recipe','recipe':'disabled-script'}, {'type':'unlock-recipe','recipe':'alternative'}]},
                          'b':{'prerequisites':['a'],'effects':[{'type':'unlock-recipe','recipe':'locked'}, {'type':'unlock-recipe','recipe':'alternative'}]}}}
    with tempfile.TemporaryDirectory() as directory:
        p=Path(directory)/'raw.json'; p.write_text(json.dumps(raw))
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
    print('PASS: script overrides, OR unlocks, prerequisites, disabled research, unknown/mismatched snapshots')

async def main():
    check_gate_semantics()
    import check_planner
    check_planner.synthetic()
    db=Database()
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'server.py')])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            listing=await session.list_tools()
            assert len(listing.tools)==14
            async def call(name,args,error=False):
                result=await session.call_tool(name,args)
                assert bool(result.isError)==error, result
                return [json.loads(c.text) for c in result.content if c.type=='text'] if not error else result
            context=(await call('get_progress_context',{}))[0]
            assert context['compatible']
            force='faction-a632079'
            recipe=(await call('get_recipe',{'name':'nullius-pressure-methanol','force':force}))[0]
            assert recipe['availability']['usable_at_stage'] is True
            unknown=(await call('get_recipe',{'name':'nullius-methanol'}))[0]
            assert unknown['availability']['state']=='unknown' # multiplayer requires a force
            related=(await call('related_recipes',{'material':'nullius-methanol','direction':'producers','available_only':True,'force':force}))[0]
            names={r['name'] for r in related['producers']}
            assert 'nullius-pressure-methanol' in names and 'nullius-fermentation' not in names
            assert not any(db.virtual(n) for n in names)
            await call('search_recipes',{'query':'methanol','available_only':True,'force':force})
            await call('production_chain',{'material':'nullius-methanol','depth':2,'max_recipes':20,'force':force})
            machines=await call('compatible_machines',{'recipe':'nullius-methanol','buildable_only':True,'force':force})
            assert machines
            await call('technology_requirements',{'technology':'nullius-high-pressure-chemistry','force':force})
            tech=(await call('get_technology',{'name':'nullius-high-pressure-chemistry','force':force}))[0]
            assert tech['state']=='researched'
            page=(await call('list_technologies',{'force':force,'state':'researched','include_hidden':True,'limit':3}))[0]
            expected=sum(t['researched'] for t in db.progress['forces'][force]['technologies'].values())
            assert page['total']==expected and len(page['technologies'])==3
            validation=(await call('validate_plan',{'recipe_rates':{'nullius-fermentation':1},'force':force}))[0]
            assert not validation['valid_at_stage']
            await call('net_balance',{'recipe_rates':{'nullius-fermentation':1},'force':force},error=True)
            await call('production_chain',{'material':'nullius-methanol'},error=True)
            balance=(await call('net_balance',{'recipe_rates':{'nullius-methane':2,'nullius-methanol':3},'force':force}))[0]
            assert balance=={'fluid:nullius-carbon-dioxide':-64,'fluid:nullius-hydrogen':-220,'fluid:nullius-oxygen':-24,'fluid:nullius-water':16,'fluid:nullius-methanol':6}
            await call('net_balance',{'recipe_rates':{'nullius-fermentation':1},'validate_stage':False})
            await call('net_balance',{'recipe_rates':{'nullius-methanol':-1},'validate_stage':False},error=True)
            equipment=(await call('validate_plan',{'recipe_rates':{'nullius-methanol':1},'machines':['nullius-chemical-plant-3'],'force':force}))[0]
            assert not equipment['valid_at_stage']
            lp=(await call('solve_production',{'targets':{'nullius-methanol':600},'per':'minute','force':force}))[0]
            assert lp['max_balance_error']<1e-6 and lp['solver']=='lp' and lp['lines']
            mx=(await call('solve_production',{'targets':{'nullius-methanol':600},'per':'minute','force':force,'solver':'matrix',
                                               'lines':lp['lines_for_matrix'],**lp['matrix_args']}))[0]
            assert abs(mx['totals']['machines']-lp['totals']['machines'])<1e-6
            await call('solve_production',{'targets':{'nullius-methanol':1},'solver':'matrix','force':force},error=True)
            stats=(await call('machine_stats',{'recipe':'nullius-methanol','rate':10,'force':force}))[0]
            assert stats['valid_at_stage'] and stats['for_rate']['machines']>0
            pm=(await call('production_matrix',{'lines':[l['recipe'] for l in lp['lines_for_matrix']],'targets':{'nullius-methanol':10}}))[0]
            assert pm['square_system']['items']==len(pm['items'])
    cases=json.loads((ROOT/'data/methanol-current-stage.json').read_text())
    for c in cases:
        assert c['stage_validation']['valid_at_stage'] and c['max_balance_error']<1e-6
    print('PASS: all 14 stdio MCP tools, pagination, force selection, locked-recipe/machine rejection, explicit theory mode, current-stage balances')

if __name__=='__main__': asyncio.run(main())
