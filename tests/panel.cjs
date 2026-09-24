// Run with PLAYWRIGHT_MODULE pointing to an installed playwright package.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1400,height:1000}});
    page.setDefaultTimeout(5000);
    const errors=[];
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent('<style>body{margin:0;font-family:Arial;--primary-text-color:#222;--secondary-text-color:#666;--divider-color:#ddd;--card-background-color:#fff;--secondary-background-color:#f4f4f4;--primary-color:#1976d2}</style>');
    await page.addScriptTag({path:process.env.PANEL_SOURCE || path.join(__dirname,"../custom_components/ld2410_tuner/static/ld2410-tuner-panel.js")});
    await page.evaluate(() => {
      window.requests=[];
      window.fail=false;
      window.race=false;
      window.fixture={devices:Object.fromEntries(["a","b","c"].map(id=>[id,{
        name:`Room ${id}`, sample_counts:{g0_move:{present:100,not_present:100}},
        current_thresholds:{g0_move:20},
        last_learning:{method:"human_priority_v3",status:"ok",automatic_evidence:{samples:{present:10,not_present:20},mean_confidence:{present:.73,not_present:.85},effective_weight:{present:1.46,not_present:3.4},deferred_samples:0},proposals:{g0_move:{threshold:15,status:"ok",sensitivity:1,not_present_samples:100,noise_ceiling:5,noise_floor_p99:5}},training:{sensitivity:1,false_positive_rate:0,present_samples:100,not_present_samples:100},validation:{sensitivity:1,false_positive_rate:0,present_samples:20,not_present_samples:20}},
        history:{labels:[]},auto_learning:{observations:14,last:{state:"present",confidence:.73,basis:"human-guided"}},
      }]))};
      const panel=document.createElement("ld2410-tuner-panel");
      window.panel=panel;
      document.body.append(panel);
      panel.hass={callWS:async message=>{
        window.requests.push(message);
        if(message.type.endsWith("snapshot")) return structuredClone(window.fixture);
        if(message.type.endsWith("history_series_multi")) {
          if(window.fail) throw new Error("History offline");
          if(window.race) await new Promise(resolve=>setTimeout(resolve,message.hours===1?150:10));
          if(window.delay) await new Promise(resolve=>setTimeout(resolve,window.delay));
          const end=message.end ?? Date.now()/1000,start=end-message.hours*3600;
          return {start,end,bucket_seconds:message.hours<=6?60:300,labels:[],series:Object.fromEntries(message.keys.map(key=>[key,{points:[{t:start+60,min:5,avg:10,max:20},{t:end-60,min:6,avg:11,max:22}],sample_count:2}]))};
        }
        return {};
      }};
    });
    const cards=page.locator('.card');
    await cards.first().waitFor();
    assert.equal(await cards.count(),3);
    assert.match(await cards.first().locator(".name").innerText(),/73%/);
    await cards.first().locator('[data-action="toggle"]').click();
    await cards.first().locator('svg').waitFor();
    for(let i=0;i<3;i++){
      await cards.first().locator('[data-action="toggle"]').click();
      await cards.first().locator('[data-action="toggle"]').click();
      await page.evaluate(()=>panel._load());
      assert.equal(await cards.count(),3,"all cards survive polling after chart render");
      assert.equal(await cards.first().locator('svg').count(),1);
    }
    await cards.nth(1).locator('[data-action="toggle"]').click();
    await cards.nth(1).locator('svg').waitFor();
    assert.equal(await cards.count(),3);
    await cards.first().locator('[data-action="section-toggle"][data-section="chart"]').click();
    await cards.first().locator('[data-action="section-toggle"][data-section="chart"]').click();
    assert.equal(await cards.count(),3);

    await cards.first().locator('[data-action="section-toggle"][data-section="history"]').click();
    const from=cards.first().locator('[data-action="history-start"]');
    await from.fill("2026-09-23T12:00");
    await from.blur();
    await page.evaluate(()=>panel._load());
    assert.equal(await from.inputValue(),"2026-09-23T12:00","draft survives refresh");

    await cards.first().locator('[data-action="section-toggle"][data-section="auto"]').click();
    assert.match(await cards.first().locator('.auto-head').innerText(),/20% × confidence/);
    assert.match(await cards.first().locator('.auto-details').innerText(),/73%/);
    await cards.first().locator('[data-action="section-toggle"][data-section="details"]').click();
    assert.match(await cards.first().locator('[data-section="details"] .subsection-body').innerText(),/99.9%/);
    assert.match(await cards.first().locator('[data-section="details"] .subsection-body').innerText(),/1.5 \/ 3.4 human-sample equivalents/);
    await page.evaluate(()=>{fixture.devices.b.last_learning.status="ok";fixture.devices.b.last_learning.validation=null;fixture.devices.b.last_learning.training=null;fixture.devices.b.last_learning.evidence_basis="automatic";fixture.devices.b.last_learning.proposals.g0_move.evidence_basis="automatic";return panel._load();});
    assert.match(await cards.nth(1).locator('.learn-status').innerText(),/estimated threshold/);
    assert.equal(await cards.nth(1).locator('[data-action="apply"]').isDisabled(),false);

    // A real focused selector must render the response without needing blur.
    const range=cards.first().locator('[data-action="chart-range"]');
    const anchor=await page.evaluate(()=>panel._chartState.get("a").end);
    await page.evaluate(()=>{window.delay=100;window.savedRange=panel.shadowRoot.querySelector('[data-action="chart-range"]');});
    await range.focus();
    await range.selectOption("24");
    await page.waitForFunction(()=>!panel._chartState.get("a").loading && panel._chartState.get("a").loadedHours===24);
    assert.equal(await cards.first().locator('svg').count(),1,"focused range selector cannot strand completed chart on Loading");
    assert.equal(await page.evaluate(()=>panel.shadowRoot.activeElement===window.savedRange),true);
    assert.equal(await page.evaluate(()=>panel._chartState.get("a").end),anchor,"range changes retain the same end time");
    const requestCount=await page.evaluate(()=>requests.filter(r=>r.type.endsWith("history_series_multi")).length);
    await range.selectOption("6");
    assert.equal(await page.evaluate(()=>panel._chartState.get("a").loadedHours),6);
    assert.equal(await page.evaluate(()=>requests.filter(r=>r.type.endsWith("history_series_multi")).length),requestCount,"switching back uses cached data");
    await range.blur();
    await page.evaluate(()=>{window.savedSvg=panel.shadowRoot.querySelector("svg");return panel._load();});
    assert.equal(await page.evaluate(()=>panel.shadowRoot.querySelector("svg")===window.savedSvg),true,"unchanged chart is not rebuilt on snapshot polling");
    assert.equal(await page.evaluate(()=>panel.shadowRoot.querySelector('[data-action="chart-range"]')===window.savedRange),true,"polling preserves chart controls");

    const canvasHeight=await cards.first().locator('.chart-canvas').evaluate(el=>el.getBoundingClientRect().height);
    const refresh=cards.first().locator('[data-action="chart-refresh"]');
    const buttonBefore=await refresh.evaluate(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y+window.scrollY}));
    await refresh.click();
    assert.equal(await cards.first().locator('svg').count(),1,"refresh retains the existing graph while loading");
    assert.equal(await cards.first().locator('.chart-canvas').evaluate(el=>el.getBoundingClientRect().height),canvasHeight);
    await page.waitForFunction(()=>!panel._chartState.get("a").loading);
    assert.deepEqual(await refresh.evaluate(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y+window.scrollY})),buttonBefore,"refresh stays in place");
    await page.evaluate(()=>{window.delay=0;});

    // Race two requests: the slow old range must not replace the selected one.
    await page.evaluate(()=>{window.race=true;const cs=panel._chartState.get("a");cs.hours=1;panel._fetchChartData("a");cs.hours=24;panel._fetchChartData("a");});
    await page.waitForFunction(()=>panel._chartState.get("a").loadedHours===24);
    await page.waitForTimeout(200);
    assert.equal(await page.evaluate(()=>panel._chartState.get("a").loadedHours),24);
    assert.equal(await cards.first().locator('[data-action="chart-range"]').inputValue(),"24","range control matches the displayed window");

    // A rendering error stays inside its own chart.
    await page.evaluate(()=>{const cs=panel._chartState.get("a");cs.data.series.g0_move.points=null;panel._draw();panel._renderChartCanvas("a",true);});
    assert.equal(await cards.count(),3);
    assert.match(await cards.first().locator('.chart-canvas').innerText(),/Unable to render/);
    await page.evaluate(()=>{window.race=false;panel._chartState.get("a").data=null;return panel._fetchChartData("a",true);});

    await page.evaluate(()=>{window.fail=true;return panel._fetchChartData("a",true);});
    assert.match(await cards.first().locator('.chart-status').innerText(),/History offline/);
    assert.equal(await cards.first().locator("svg").count(),1,"failed refresh retains previous graph");
    await page.evaluate(()=>{window.fail=false;return panel._fetchChartData("a",true);});
    await cards.first().locator('svg').waitFor();

    // A history timeout must settle loading and leave the previous chart usable.
    await page.evaluate(async()=>{
      const original=panel._hass.callWS;
      const originalCall=panel._call;
      panel._hass.callWS=message=>message.type.endsWith("history_series_multi")?new Promise(()=>{}):original(message);
      panel._call=function(type,data,timeout){return originalCall.call(this,type,data,type==="history_series_multi"?40:timeout);};
      try { await panel._fetchChartData("a",true); }
      finally { panel._hass.callWS=original;panel._call=originalCall; }
    });
    assert.equal(await page.evaluate(()=>panel._chartState.get("a").loading),false);
    assert.match(await cards.first().locator('.chart-status').innerText(),/timed out/);
    assert.equal(await cards.first().locator('svg').count(),1);
    await page.evaluate(()=>panel._fetchChartData("a",true));

    // Initial drag rectangle, selection persistence, and cancellation cleanup.
    const hit=cards.first().locator('[data-role="selection-hit"]');
    const box=await hit.boundingBox();
    await page.mouse.move(box.x+30,box.y+30);await page.mouse.down();await page.mouse.move(box.x+120,box.y+30);
    assert.equal(await page.evaluate(()=>panel._dragging),true);
    assert.equal(await cards.first().locator('[data-role="selection-rect"]').isVisible(),true);
    await page.mouse.up();
    assert.equal(await cards.first().locator('[data-role="handle-start"]').count(),1);
    await page.evaluate(()=>panel._load());
    assert.equal(await cards.first().locator('[data-role="handle-start"]').count(),1);
    await hit.dispatchEvent("pointerdown",{clientX:box.x+50,clientY:box.y+30});
    await page.evaluate(()=>window.dispatchEvent(new Event("pointercancel")));
    assert.equal(await page.evaluate(()=>panel._dragging),false);

    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:"/tmp/ld2410-mobile.png",fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,"mobile no horizontal overflow");
    await page.setViewportSize({width:1400,height:1000});
    await page.screenshot({path:"/tmp/ld2410-desktop.png",fullPage:true});
    await page.evaluate(()=>{panel.remove();});
    assert.equal(await page.evaluate(()=>panel._pollTimer),null);
    await page.evaluate(()=>document.body.append(panel));
    assert.equal(await page.evaluate(()=>Boolean(panel._pollTimer)),true);
    assert.deepEqual(errors,[]);
    console.log("PASS: focused range changes, stable window endpoints, cached range switching, stable refresh geometry, retained graph on refresh/error, chart controls across polling, cards, races, drafts, selection, mobile layout, reconnect; no browser errors");
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
