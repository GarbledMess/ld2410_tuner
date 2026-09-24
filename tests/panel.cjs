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
        last_learning:{method:"joint_temporal_v2",status:"ok",automatic_evidence:{samples:{present:10,not_present:20},mean_confidence:{present:.73,not_present:.85},effective_weight:{present:1.46,not_present:3.4},deferred_samples:2},proposals:{g0_move:{threshold:15,status:"ok",sensitivity:1,not_present_samples:100,noise_ceiling:5,noise_floor_p99:5}},validation:{sensitivity:1,false_positive_rate:0,present_samples:20,not_present_samples:20}},
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
          const end=Date.now()/1000,start=end-message.hours*3600;
          return {start,end,labels:[],series:Object.fromEntries(message.keys.map(key=>[key,{points:[{t:start+60,min:5,avg:10,max:20},{t:end-60,min:6,avg:11,max:22}],sample_count:2}]))};
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
    await page.evaluate(()=>{fixture.devices.b.last_learning.status="provisional";fixture.devices.b.last_learning.validation=null;fixture.devices.b.last_learning.proposals.g0_move.status="provisional";return panel._load();});
    assert.match(await cards.nth(1).locator('.learn-status').innerText(),/provisional threshold/);
    assert.equal(await cards.nth(1).locator('[data-action="apply"]').isDisabled(),true);

    // Race two requests: the slow old range must not replace the selected one.
    await page.evaluate(()=>{window.race=true;const cs=panel._chartState.get("a");cs.hours=1;panel._fetchChartData("a");cs.hours=24;panel._fetchChartData("a");});
    await page.waitForFunction(()=>panel._chartState.get("a").loadedHours===24);
    await page.waitForTimeout(200);
    assert.equal(await page.evaluate(()=>panel._chartState.get("a").loadedHours),24);

    // A rendering error stays inside its own chart.
    await page.evaluate(()=>{const cs=panel._chartState.get("a");cs.data.series.g0_move.points=null;panel._draw();});
    assert.equal(await cards.count(),3);
    assert.match(await cards.first().locator('.chart-canvas').innerText(),/Unable to render/);
    await page.evaluate(()=>{window.race=false;panel._chartState.get("a").data=null;return panel._fetchChartData("a");});

    await page.evaluate(()=>{window.fail=true;return panel._fetchChartData("a");});
    assert.match(await cards.first().locator('.chart-canvas').innerText(),/History offline/);
    await page.evaluate(()=>{window.fail=false;return panel._fetchChartData("a");});
    await cards.first().locator('svg').waitFor();

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
    console.log("PASS: charts, 3-card collapse/expand, poll redraw, drafts, request races, error isolation/retry, selection/cancellation, mobile layout, reconnect; no browser errors");
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
