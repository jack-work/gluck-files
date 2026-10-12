#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import subprocess
import sys

spec=importlib.util.spec_from_file_location('media_stack',pathlib.Path(__file__).with_name('test_media_stack.py'))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
stack=module.Stack
try:
    stack.setUpClass()
    audio=pathlib.Path(os.environ.get('MEDIA_FILE') or module.ROOT/'long.mp3')
    if not os.environ.get('MEDIA_FILE'):
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3600',
                    '-c:a','libmp3lame','-b:a','64k',str(audio)],check=True,timeout=60)
    with audio.open('rb') as body:
        stack.s3.put_object(Bucket='files',Key='dad/long.mp3',Body=body,ContentType='audio/mpeg')
    script=module.ROOT/'browser.cjs'
    script.write_text(r'''
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_BIN,args:[
   '--no-sandbox','--no-proxy-server','--autoplay-policy=no-user-gesture-required',
   '--host-resolver-rules=MAP files.kelliher.info 127.0.0.1']});
 try {
  const context=await browser.newContext();
  await context.addCookies([{name:'user',value:'alice',domain:'files.kelliher.info',path:'/'}]);
  const page=await context.newPage();
  const ranges=[];
  const started=Date.now();
  page.on('request',async r=>{if(r.url().includes('/dad/long.mp3')){const h=await r.allHeaders();ranges.push({seconds:(Date.now()-started)/1000,range:h.range||null,hasCookie:!!h.cookie});}});
  const replies=[];page.on('response',r=>{if(r.url().includes('/dad/long.mp3'))replies.push({seconds:(Date.now()-started)/1000,status:r.status()});});
  page.on('console',m=>console.log('browser:',m.text()));
  const cdp=await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled',{cacheDisabled:true});
  await cdp.send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:16000,uploadThroughput:1000000});
  await page.goto('http://files.kelliher.info:28780/_files/view?uri=%2Fdad%2Flong.mp3',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('audio,video',{timeout:15000});
  console.log('native-player',await page.locator('audio,video').evaluate(m=>({tag:m.tagName,current:m.currentTime,ready:m.readyState,error:m.error&&m.error.code})));
  await page.locator('audio,video').evaluate(m=>Promise.race([m.play(),new Promise((_,reject)=>setTimeout(()=>reject(Error('play did not start')),15000))]));
  if(process.env.MEDIA_SMOKE==='1') {
    await page.waitForFunction(()=>document.querySelector('audio,video').currentTime>2,{},{timeout:15000});
  } else {
    await page.waitForFunction(()=>document.querySelector('audio,video').currentTime>125,{},{timeout:160000});
    console.log('past-120',await page.locator('audio,video').evaluate(m=>m.currentTime));
    const target=await page.locator('audio,video').evaluate(m=>Math.floor(m.duration*0.85));
    await page.locator('audio,video').evaluate((m,t)=>{m.currentTime=t;},target);
    await page.waitForFunction(t=>{let m=document.querySelector('audio,video');return m.currentTime>t+2&&!m.seeking&&m.readyState>=3;},target,{timeout:30000});
    if(!ranges.some(r=>r.seconds>120&&r.range&&r.range!=='bytes=0-'&&r.hasCookie))throw Error('seek used no fresh late Range with cookie');
    if(!replies.some(r=>r.seconds>120&&r.status===206))throw Error('late Range did not return206');
  }
  console.log('player-ok',await page.locator('audio,video').evaluate(m=>({time:m.currentTime,error:m.error&&m.error.code,ready:m.readyState})),JSON.stringify(ranges),JSON.stringify(replies));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
''')
    subprocess.run(['node',str(script)],env=os.environ.copy(),check=True,timeout=230)
finally:
    if hasattr(stack,'auth'):stack.auth.shutdown();stack.auth.server_close()
    if hasattr(stack,'lister'):
        stack.lister.task_dispatcher.shutdown(cancel_pending=True,timeout=1)
        for channel in list(stack.lister._map.values()):channel.close()
        stack.lister.close()
    for process in reversed(module.PROCESSES):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    import shutil
    shutil.rmtree(module.ROOT,ignore_errors=True)
