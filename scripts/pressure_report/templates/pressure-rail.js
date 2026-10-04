// Railway estimates use actual fluid units, independently of table display units.
const railDefaults={railSource:'manual',railStage:'0',railPreset:'integrated',railWagon:'auto',railCars:'4',railFill:'100',railCycle:'180',railAvailability:'85',railPumps:'2',railClearance:'15',railStationUtil:'80',railBuffer:'120',railFilter:'suggested'};
const railSuggestions={
 'nullius-volcanic-gas':[100,'矿区 → 碳源区（按外部供给量）'],
 'nullius-compressed-hydrogen':[25,'公用工程 → 化工 / 冶金；优先相邻管网'],
 'nullius-compressed-carbon-monoxide':[40,'碳源 / 冶金 → 有机化工'],
 'nullius-compressed-carbon-dioxide':[25,'副产回收 → 碳源区；碳循环尽量同区'],
 'nullius-compressed-methane':[40,'有机副产 → 碳源区'],
 'nullius-compressed-nitrogen':[25,'空分 → 化工'],
 'nullius-compressed-oxygen':[0,'优先同区回用，过剩本地排放'],
 'nullius-compressed-argon':[50,'空分 → 冶金 / 硅'],
 'nullius-compressed-air':[0,'空气本地取用，通常不走铁路'],
 'nullius-ethylene':[50,'有机化工 → 聚合物（跨区时）'],
 'nullius-acid-sulfuric':[50,'酸碱区 → 矿物加工（跨区时）'],
 'nullius-steam':[0,'就地制汽、电解与冷凝闭环'],
 'nullius-pressure-steam':[0,'就地热源，避免跨区大流量蒸汽']
};
let railShares={};
function applyRailPreset(preset){railShares=Object.fromEntries(allFluids.map(id=>[id,preset==='local'?(id==='nullius-volcanic-gas'?100:0):preset==='remote'?(railSuggestions[id]?.[0]>0?100:0):(railSuggestions[id]?.[0]||0)]));}
function resetRail(){for(const[id,v]of Object.entries(railDefaults))$(id).value=v;applyRailPreset('integrated');}
function wagonFor(p){let available=DATA.rail.filter(w=>w.currently_available||p.new_technologies.some(t=>t.technology===w.technology));return $('railWagon').value==='auto'?available.at(-1):DATA.rail[+$('railWagon').value-1];}
function railValid(){const ranges={railCars:[1,32],railFill:[1,100],railCycle:[1,7200],railAvailability:[1,100],railPumps:[1,3],railClearance:[0,600],railStationUtil:[1,100],railBuffer:[0,7200]};let valid=true;for(const[id,[min,max]]of Object.entries(ranges)){const n=+$(id).value,ok=$(id).value!==''&&Number.isFinite(n)&&n>=min&&n<=max&&(!['railCars','railPumps'].includes(id)||Number.isInteger(n));$(id).setAttribute('aria-invalid',String(!ok));valid&&=ok;}$('railError').textContent=valid?'':'请填写范围内的有效铁路条件；车厢数和泵数必须为整数。当前保留上一次有效铁路结果。';return valid;}
function railModel(s){let p=s.plans[+$('railStage').value],wagon=wagonFor(p),cars=+$('railCars').value,payload=wagon.capacity*cars*+$('railFill').value/100,dwell=payload/(cars*+$('railPumps').value*capacity(p)*s.util)+ +$('railClearance').value,cycle=Math.max(+$('railCycle').value,2*dwell),available=+$('railAvailability').value/100,stationUtil=+$('railStationUtil').value/100;
 const source=$('railSource').value,net=source==='manual'?null:Object.fromEntries(DATA.zoning.scenarios[p.name][source.replace('zone-','')].materials.map(m=>[m.material,m.total_import]));
 let lines=allFluids.map(id=>{let base=net?(net['fluid:'+id]||0):id==='nullius-volcanic-gas'?p.supplies['fluid:nullius-volcanic-gas']:flow(p,id),q=base*s.scale*(railShares[id]||0)/100,freq=q/payload;return{id,q,freq,trains:ceil(freq*cycle/available),berths:ceil(freq*dwell/stationUtil),buffer:q>1e-9?Math.max(payload,q*+$('railBuffer').value):0};});return{p,wagon,payload,dwell,cycle,lines};}
function renderRail(s){if(!railValid())return;const m=railModel(s),active=m.lines.filter(r=>r.q>1e-9),sum=k=>active.reduce((n,r)=>n+r[k],0),freq=sum('freq')*60;
 const unlocked=m.wagon.currently_available||m.p.new_technologies.some(t=>t.technology===m.wagon.technology);
 $('railSpec').textContent=`${'ABC'[+$('railStage').value]} · ${labels[m.p.name]} · ${name(m.wagon.name,'entity-name')} ${fmt(m.wagon.capacity,0)}/厢 · 每列计划载荷 ${fmt(m.payload,0)} · 每端理想服务 ${fmt(m.dwell)} 秒 · 计算往返 ${fmt(m.cycle)} 秒。${!unlocked?'此车型尚未在该阶段解锁，仅用于未来规划。':''}`;
 $('railStats').innerHTML=`<div>全部选定线路<strong>${fmt(freq,2)}</strong>重载发车 /min · ${active.length} 种流体</div><div>按流体专用车队<strong>${fmt(sum('trains'),0)} 列</strong>${fmt(sum('trains')*+$('railCars').value,0)} 节流体车厢 · 机车另计</div><div>装货 / 卸货站台总数<strong>${fmt(sum('berths'),0)} / ${fmt(sum('berths'),0)}</strong>理想并联装卸估计 · 不含候车线</div>`;
 $('railWarning').textContent=(m.cycle>+$('railCycle').value?'填写周期短于两端最低服务时间，计算已自动提高到 '+fmt(m.cycle)+' 秒。 ':'')+(freq>0?`若所有线路都经过同一处双向汇流口，含空返约 ${fmt(freq*2,2)} 列次/min，相邻列次平均 ${fmt(30/freq,2)} 秒；这只是交通压力指标，请按走廊分流，并用实际路网测试容量。`:'当前没有铁路运量。');
 let filter=$('railFilter').value,lines=m.lines.filter(r=>filter==='all'||filter==='active'&&r.q>1e-9||filter==='suggested'&&(railSuggestions[r.id]||(railShares[r.id]||0)>0));lines.sort((a,b)=>b.q-a.q||a.id.localeCompare(b.id));
 $('railRows').innerHTML=lines.length?lines.map(r=>`<tr><td style="min-width:210px">${esc(name(r.id))}<small>${esc(railSuggestions[r.id]?.[1]||'自行指定分区流向')}</small></td><td><input style="width:76px" type="number" min="0" max="100" step="any" value="${railShares[r.id]||0}" data-rail-share="${r.id}" aria-label="${esc(name(r.id))}铁路比例"></td><td class="number">${fmt(r.q)}</td><td class="number">${fmt(r.freq*60,3)}</td><td class="number">${r.freq>0?fmt(1/r.freq):'—'}</td><td class="number">${r.trains}</td><td class="number">${r.berths} / ${r.berths}</td><td class="number">${fmt(r.buffer,0)}</td></tr>`).join(''):'<tr><td class="empty" colspan="8">没有铁路运量，可切换“所有流体”设置运输比例。</td></tr>';
}
for(const id of Object.keys(railDefaults))$(id).addEventListener($(id).tagName==='SELECT'?'change':'input',()=>{if(id==='railPreset'&&$(id).value!=='custom')applyRailPreset($(id).value);if(validate())renderRail(state());});
$('railRows').addEventListener('change',e=>{const input=e.target.closest('[data-rail-share]');if(!input)return;const n=+input.value;if(input.value===''||!Number.isFinite(n)||n<0||n>100){input.setAttribute('aria-invalid','true');$('railError').textContent='每种流体的铁路比例必须为 0–100%；当前保留该项上一次有效比例。';return;}railShares[input.dataset.railShare]=n;$('railPreset').value='custom';if(validate())renderRail(state());});
resetRail();
