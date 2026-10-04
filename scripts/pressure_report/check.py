"""Functional browser checks for the offline report. Requires Python Playwright + Edge."""
import csv
import io
import json
import math
from pathlib import Path

from playwright.sync_api import sync_playwright

from recipe_mcp.paths import DATA_DIR, WORKSPACE_ROOT


def check() -> None:
    source = json.loads((DATA_DIR / 'pressure-transition-all.json').read_text(encoding='utf-8'))
    source = {s['name']: s for s in source}
    hydrogen = next(f['process_demand_per_second'] for f in source['entry_volcanic']['fluid_bus']
                    if f['fluid'] == 'nullius-compressed-hydrogen')
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True, timeout=15000)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto((WORKSPACE_ROOT / 'docs/nullius-pressure-stage-plan.html').as_uri(), wait_until='domcontentloaded')
        assert page.locator('#cards article').count() == 3
        assert page.locator('#fluidRows tr').count() == 46

        def hrow() -> list[str]:
            return page.locator('tr[data-fluid="nullius-compressed-hydrogen"] td').all_text_contents()

        def number(value: str) -> float:
            return float(value.split('台泵')[0].split('\n')[0].replace(',', '').strip())

        assert f'{hydrogen:,.1f}' in hrow()[1]
        assert '17 台泵' in hrow()[1]
        page.locator('#rate').fill('75')
        assert f'{hydrogen / 2:,.1f}' in hrow()[1]
        assert '9 台泵' in hrow()[1]
        page.locator('#rateUnit').select_option('60')
        assert page.locator('#rate').input_value() == '4500'
        assert f'{hydrogen / 2:,.1f}' in hrow()[1]
        page.locator('#viewUnit').select_option('equivalent')
        assert f'{hydrogen * 2:,.1f}' in hrow()[1]
        assert '9 台泵' in hrow()[1]
        page.locator('#flowUnit').select_option('60')
        assert f'{hydrogen * 120:,.1f}' in hrow()[1]
        assert '9 台泵' in hrow()[1]
        page.locator('#reset').click()

        # All pre-solved variants switch correctly, including future equipment gating.
        for control, names in [('entryRoute', ['entry_water_electrolysis', 'entry_volcanic']),
                               ('midRoute', ['mechanical_upgrade', 'science_upgrades', 'industrial_volcanic', 'industrial_capped']),
                               ('carbonRoute', ['carbon_without_new_recipes', 'carbon_volcanic'])]:
            for scenario in names:
                page.locator('#' + control).select_option(scenario)
                assert page.locator('#validation').inner_text() == ''
        page.locator('#category').select_option('compressed')
        assert page.locator('#fluidRows tr').count() == 11
        page.locator('#search').fill('hydrogen')
        assert page.locator('#fluidRows tr').count() == 1
        page.locator('#search').fill('不存在的流体')
        assert '没有符合' in page.locator('#fluidRows').inner_text()
        page.locator('#quick').select_option('expansion')
        assert page.locator('#focusStage').input_value() == '2'
        assert page.evaluate('rows.every(r => r.delta > 0)')
        with page.expect_download() as download_info:
            page.locator('#export').click()
        content = Path(download_info.value.path()).read_text(encoding='utf-8-sig')
        records = list(csv.reader(io.StringIO(content)))
        assert len(records) == page.locator('#fluidRows tr').count() + 1
        assert records[0][2] == '每种瓶/s'
        assert all(float(r[2]) == 150 for r in records[1:])
        page.locator('#reset').click()
        page.locator('#utilization').fill('0')
        assert page.locator('#validation').inner_text()
        assert page.locator('#export').is_disabled()
        page.locator('#utilization').fill('80')
        page.locator('#volcanoCapacity').fill('60000')
        assert '供应缺口' in page.locator('#cards article').nth(0).inner_text()
        assert '供应余量' in page.locator('#cards article').nth(2).inner_text()

        # Independent transport math for the external volcanic supply.
        page.locator('#tab-rail').click()
        page.locator('#railPreset').select_option('local')
        model = page.evaluate('railModel(state())')
        volcanic = next(r for r in model['lines'] if r['id'] == 'nullius-volcanic-gas')
        q = source['entry_volcanic']['supplies']['fluid:nullius-volcanic-gas']
        payload = 80000 * 4
        dwell = payload / (4 * 2 * 6000 * .8) + 15
        assert model['payload'] == payload
        assert math.isclose(volcanic['q'], q)
        assert volcanic['trains'] == math.ceil(q / payload * 180 / .85)
        assert volcanic['berths'] == math.ceil(q / payload * dwell / .8)
        assert math.isclose(volcanic['buffer'], q * 120)
        page.locator('#viewUnit').select_option('equivalent')
        page.locator('#flowUnit').select_option('60')
        assert page.evaluate('railModel(state()).lines.find(r=>r.id==="nullius-volcanic-gas").q') == q
        page.locator('#railCycle').fill('1')
        assert '自动提高' in page.locator('#railWarning').inner_text()
        page.locator('#railStage').select_option('2')
        assert page.evaluate('railModel(state()).wagon.capacity') == 250000
        page.locator('#railStage').select_option('0')
        page.locator('#railWagon').select_option('3')
        assert '尚未在该阶段解锁' in page.locator('#railSpec').inner_text()
        page.locator('#railWagon').select_option('auto')
        page.locator('#railPreset').select_option('integrated')
        share = page.locator('[data-rail-share="nullius-compressed-hydrogen"]')
        share.fill('50')
        share.press('Tab')
        assert page.locator('#railPreset').input_value() == 'custom'
        assert math.isclose(page.evaluate('railModel(state()).lines.find(r=>r.id==="nullius-compressed-hydrogen").q'), hydrogen / 2)
        page.locator('#railCars').fill('0')
        assert page.locator('#railError').inner_text()
        page.locator('#railCars').fill('4')
        assert page.locator('#railError').inner_text() == ''

        page.locator('#tab-recipes').click()
        page.locator('#recipeStage').select_option('2')
        page.locator('#recipeFilter').select_option('removed')
        assert page.locator('#recipeRows tr').count() > 1
        assert all('本方案停用' in t for t in page.locator('#recipeRows tr').all_text_contents())
        page.locator('#tab-method').click()
        page.locator('#techStage').select_option('2')
        assert page.locator('.tech-item').count() == 116
        page.locator('#techSearch').fill('carbon-sequestration')
        assert 0 < page.locator('.tech-item').count() < 116

        # Partition interfaces are net balances, with exact recipe/equipment allocation.
        page.locator('#tab-zones').click()
        assert page.locator('#zoneMap button').count() == 6
        for layout, count in [('rail', 6), ('bus', 9)]:
            page.locator('#zoneLayout').select_option(layout)
            assert page.locator('#zoneMap button').count() == count
            for stage in ['0', '1', '2']:
                page.locator('#zoneStage').select_option(stage)
                assert page.locator('#zoneEquipment>div').count() > 0
                assert page.locator('#zoneRecipes details').count() > 0
        page.locator('#zoneLayout').select_option('rail')
        page.locator('[data-zone="carbon"]').click()
        page.locator('#zoneRecipeSearch').fill('carbon-deposition')
        assert page.locator('#zoneRecipes details').count() == 1
        page.locator('#zoneRecipes summary').click()
        assert '40' in page.locator('#zoneRecipes .zone-formula').inner_text()
        page.locator('#zoneRecipeSearch').fill('')
        page.locator('#zoneRecipeFilter').select_option('removed')
        assert page.locator('#zoneRecipes details').count() > 0
        assert '本方案停用' in page.locator('#zoneRecipes').inner_text()
        page.locator('#zoneRecipeFilter').select_option('active')
        page.locator('#zoneMaterialType').select_option('fluid')
        assert 'item:' not in page.locator('#zoneInputs').inner_text()
        with page.expect_download() as zone_download:
            page.locator('#zoneExport').click()
        exported = json.loads(Path(zone_download.value.path()).read_text(encoding='utf-8'))
        assert exported['scenario'] == 'carbon_volcanic'
        assert exported['rate_each_pack_per_second'] == 150
        assert exported['net_per_second']['fluid:nullius-volcanic-gas'] < 0
        page.locator('#zoneToRail').click()
        assert page.locator('#railSource').input_value() == 'zone-rail'
        assert page.locator('#railStage').input_value() == '2'
        assert page.evaluate('railModel(state()).lines.find(r=>r.id==="nullius-compressed-hydrogen").q === DATA.zoning.scenarios.carbon_volcanic.rail.materials.find(m=>m.material==="fluid:nullius-compressed-hydrogen").total_import')
        page.locator('#tab-zones').click()
        page.locator('#zoneRecipeFilter').select_option('active')
        page.screenshot(path=str(WORKSPACE_ROOT / 'docs/pressure-dashboard-zones.png'), full_page=True)

        # Zero target stays well-defined, including discrete fleets and pumps.
        page.locator('#rate').fill('0')
        assert page.evaluate('railModel(state()).lines.every(r=>r.trains===0 && r.berths===0 && r.buffer===0)')
        assert not page.locator('body').inner_text().__contains__('NaN')
        page.locator('#reset').click()
        page.screenshot(path=str(WORKSPACE_ROOT / 'docs/pressure-dashboard-desktop.png'), full_page=True)
        page.locator('#tab-rail').click()
        page.screenshot(path=str(WORKSPACE_ROOT / 'docs/pressure-dashboard-rail.png'), full_page=True)
        for width in [390, 360, 768]:
            page.set_viewport_size({'width': width, 'height': 844})
            for tab in ['fluids', 'rail', 'zones', 'recipes', 'upgrades', 'method']:
                page.locator('#tab-' + tab).click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, tab)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#tab-rail').click()
        page.screenshot(path=str(WORKSPACE_ROOT / 'docs/pressure-dashboard-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
    print('PASS: scaling, units, pumps, all routes, filters, exports, validation, rail math, 6/9-zone interfaces, recipe/technology views, zero load, and 3 responsive widths; no JS errors.')


if __name__ == '__main__':
    check()
