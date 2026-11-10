/** Spectra browser interface. Audio never leaves the device. */
const $=id=>document.getElementById(id);
const canvasSpec=$('spectrum'),canvasGram=$('spectrogram');
const specCtx=canvasSpec.getContext('2d',{alpha:false});
const gramCtx=canvasGram.getContext('2d',{alpha:false});
const worker=new Worker(new URL('./workers/dsp-worker.mjs',import.meta.url),{type:'module'});
const rgbAnchors=[
  [0,2,1,12],[.14,32,11,79],[.28,83,12,124],[.42,140,28,108],
  [.56,195,52,72],[.70,237,104,35],[.84,250,172,48],[1,252,255,164]
];
const PALETTE=new Uint8ClampedArray(256*4);
for(let i=0;i<256;i++){
  const t=i/255;let j=1;while(j<rgbAnchors.length-1&&rgbAnchors[j][0]<t)j++;
  const a=rgbAnchors[j-1],b=rgbAnchors[j],u=(t-a[0])/(b[0]-a[0]);
  for(let c=0;c<3;c++)PALETTE[i*4+c]=Math.round(a[c+1]+(b[c+1]-a[c+1])*u);
  PALETTE[i*4+3]=255;
}
const state={mode:'idle',stream:null,context:null,source:null,node:null,
  fs:48000,fft:2048,hop:512,history:[],seconds:12,axis:'log',floor:-105,ceil:-20,
  latest:null,metrics:null,dirty:true,rendererFrames:0,lastDrawAt:0,lastDrawMs:0,
  selectedChannel:1,availableChannels:0,working:false,lastDetail:'',configured:false,
  epoch:0,inflight:0,gapPending:false,droppedBlocks:0};
function status(level,text){
  const el=$('status-pill');el.className=`status ${level||''}`;
  $('status-short').textContent=text;
}
function msg(text,level=''){
  $('message').className=`message ${level}`;
  $('message').textContent=text;
}
function buttons(){
  $('start-btn').disabled=state.working||state.mode==='mic';
  $('demo-btn').disabled=state.working||state.mode==='demo';
  $('stop-btn').disabled=state.working||state.mode==='idle';
}
function setMode(mode){
  state.mode=mode;buttons();state.dirty=true;
  status(mode==='mic'?'live':mode==='demo'?'demo':'',mode==='mic'?'LIVE MICROPHONE':mode==='demo'?'DEMO SIGNAL':'READY');
  if(mode==='idle'){$('input-name').textContent='No active input';$('input-meta').textContent='Waiting for audio · No recording or upload';$('input-extra').textContent='MIC OFF';}
}
function cleanMeasurements(){
  state.history=[];state.metrics=null;state.latest=null;state.dirty=true;
  $('note').textContent='—';$('pitch-hz').textContent='NO STABLE PITCH';$('cents').innerHTML='— <span class="unit">¢</span>';
  $('confidence').innerHTML='— <span class="unit">%</span>';
  $('harmonic').innerHTML='— <span class="unit">%</span>';
  $('harmonic-db').textContent='Harmonic richness · not vocal THD';
  $('confidence-bar').style.width='0%';$('cents-marker').style.left='50%';
  $('rms-num').textContent='— dBFS';$('rms-bar').style.width='0%';
}
function configureWorker(){
  state.epoch++;state.inflight=0;state.gapPending=false;state.droppedBlocks=0;
  state.hop=Math.min(state.fft/4,512);
  worker.postMessage({type:'configure',sampleRate:state.fs,fftSize:state.fft,hopSize:state.hop,epoch:state.epoch});
  cleanMeasurements();
  $('input-extra').textContent=`STFT ${state.fft}/${state.hop} · ${state.mode.toUpperCase()}`;
  $('spectrum-meta').textContent=`fs ${state.fs} Hz · Δf ${(state.fs/state.fft).toFixed(1)} Hz`;
}
function frequencyText(hz){return hz>0?`${hz.toFixed(1)} Hz`:'— Hz';}
function formatDb(db){return `${Math.round(db)} dBFS`;}
function updateMeasurements(m){
  state.metrics=m;
  $('rms-num').textContent=formatDb(m.rmsDb);
  const fraction=Math.max(0,Math.min(100,(m.rmsDb+90)/90*100));
  $('rms-bar').style.width=`${fraction}%`;
  if(m.voiced){
    $('note').textContent=m.note;
    $('pitch-hz').textContent=frequencyText(m.f0);
    const sign=m.cents>0?'+':'';
    $('cents').innerHTML=`${sign}${m.cents.toFixed(1)} <span class="unit">¢</span>`;
    $('cents-marker').style.left=`${Math.max(0,Math.min(100,m.cents+50))}%`;
    $('confidence').innerHTML=`${(100*m.confidence).toFixed(0)} <span class="unit">%</span>`;
    $('confidence-bar').style.width=`${Math.min(100,100*m.confidence)}%`;
    $('harmonic').innerHTML=`${(m.ratio*100).toFixed(1)} <span class="unit">%</span>`;
    $('harmonic-db').textContent=`${m.ratioDb.toFixed(1)} dB · Harmonic richness, not THD`;
  }else{
    $('note').textContent='—';$('pitch-hz').textContent='NO STABLE PITCH';
    $('cents').innerHTML='— <span class="unit">¢</span>';
    $('confidence').innerHTML='— <span class="unit">%</span>';
    $('harmonic').innerHTML='— <span class="unit">%</span>';
    $('confidence-bar').style.width='0%';$('cents-marker').style.left='50%';
    $('harmonic-db').textContent='Only defined for a reliable f0';
  }
  if(m.clipped)$('signal-diagnostic').textContent='⚠ Digital clipping detected — reduce microphone/interface gain.';
  state.dirty=true;
}
worker.onmessage=(ev)=>{
  const m=ev.data;
  if(m.epoch !== undefined && m.epoch!==state.epoch)return;
  if(m.type==='gap-ack'){
    state.inflight=0;state.gapPending=false;cleanMeasurements();
    msg(`Capture recovered from a DSP backlog; dropped ${state.droppedBlocks} audio blocks.`,'warning');
    return;
  }
  if(m.type==='configured'){
    state.fs=m.fs;state.configured=true;
    $('input-meta').textContent=`fs ${m.fs} Hz · pitch observation ${m.pitchObservationMs.toFixed(0)} ms`;
    state.dirty=true;return;
  }
  if(m.type==='error'){msg(`DSP error: ${m.detail}`,'error');return;}
  if(m.type!=='data')return;
  if(m.audioAck)state.inflight=Math.max(0,state.inflight-1);
  if(state.gapPending)return; // drain old packets until reset acknowledgment
  for(const f of m.columns){
    const db=new Float32Array(f.db),t=f.pcmEnd/state.fs;
    state.history.push({t,db});state.latest=db;
  }
  // Bounded history even during long foreground sessions.
  const last=state.history.at(-1)?.t;
  if(last!==undefined){
    const cutoff=last-Math.max(25,state.seconds+2);
    while(state.history.length&&state.history[0].t<cutoff)state.history.shift();
  }
  if(m.metrics)updateMeasurements(m.metrics);
  state.dirty=true;
};
worker.onerror=(event)=>{msg(`DSP worker failed: ${event.message}`,'error');};
function detectSettings(track){
  const settings=track.getSettings?.()||{};
  const processed=['echoCancellation','noiseSuppression','autoGainControl'].filter(k=>settings[k]===true);
  if(processed.length){
    const text=`⚠ Browser processing active: ${processed.join(', ')}. Harmonic values may be altered.`;
    $('signal-diagnostic').textContent=text;msg(text,'warning');
  }else{
    $('signal-diagnostic').textContent='Requested raw audio (echo cancellation, suppression and AGC off). Browser behavior cannot be guaranteed.';
  }
  return settings;
}
async function refreshDevices(){
  if(!navigator.mediaDevices?.enumerateDevices)return;
  try{
    const devices=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='audioinput');
    const select=$('device-select'),previous=select.value;
    select.replaceChildren(new Option('System default',''));
    devices.forEach((d,i)=>select.add(new Option(d.label||`Microphone ${i+1}`,d.deviceId)));
    if([...select.options].some(o=>o.value===previous))select.value=previous;
  }catch{ /* Permission may not be granted yet. */ }
}
async function stopAll({quiet=false}={}){
  worker.postMessage({type:'stop-demo'});
  if(state.node){state.node.port.onmessage=null;try{state.node.disconnect();}catch{}state.node=null;}
  if(state.source){try{state.source.disconnect();}catch{}state.source=null;}
  if(state.stream){for(const t of state.stream.getTracks())t.stop();state.stream=null;}
  if(state.context){const c=state.context;state.context=null;try{await c.close();}catch{}}
  state.epoch++;state.inflight=0;state.gapPending=false;
  setMode('idle');
  if(!quiet)msg('Stopped. No microphone audio was recorded or uploaded.');
}
async function startDemo(){
  state.working=true;buttons();
  try{
    await stopAll({quiet:true});
    state.fs=48000;state.fft=Number($('fft-select').value);
    setMode('demo');configureWorker();
    $('input-name').textContent='Synthetic vocal-like tones · A3 / C4 / E4 / G4';
    $('input-meta').textContent='No microphone permission required';
    worker.postMessage({type:'start-demo'});
    msg('Demo running: notes change approximately every 3.5 seconds. Microphone is off.');
  }catch(err){msg(`Demo failed: ${err.message}`,'error');}
  finally{state.working=false;buttons();}
}
async function startMic(){
  if(!navigator.mediaDevices?.getUserMedia||!window.AudioWorkletNode){
    msg('This browser does not support required microphone/Web Audio APIs. Use up-to-date Safari on iPhone, or Chrome/Edge on desktop.','error');return;
  }
  if(!window.isSecureContext){msg('Microphone capture requires HTTPS. Publish to Netlify and open the HTTPS address on your iPhone.','error');return;}
  state.working=true;buttons();let ctx=null,stream=null;
  try{
    // No await before creating/resuming AudioContext and requesting the mic:
    // Safari may require these operations within the original tap gesture.
    if(state.mode==='demo'){worker.postMessage({type:'stop-demo'});setMode('idle');}
    // Create the context during the user activation. Safari may require this.
    ctx=new (window.AudioContext||window.webkitAudioContext)({latencyHint:'interactive'});
    const earlyResume=ctx.resume().catch(()=>{});
    const chosen=$('device-select').value;
    const constraints={echoCancellation:false,noiseSuppression:false,autoGainControl:false,
      channelCount:{ideal:2}};
    if(chosen)constraints.deviceId={ideal:chosen};
    try{stream=await navigator.mediaDevices.getUserMedia({audio:constraints,video:false});}
    catch(e){
      if(e.name==='OverconstrainedError'||e.name==='TypeError'){
        stream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
        msg('Advanced constraints unavailable; fell back to basic microphone capture. Check browser processing.','warning');
      }else throw e;
    }
    await ctx.audioWorklet.addModule(new URL('./workers/capture-worklet.js',import.meta.url));
    const source=ctx.createMediaStreamSource(stream);
    const node=new AudioWorkletNode(ctx,'spectra-capture',{
      numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1],channelCountMode:'max',
      processorOptions:{blockFrames:1024}
    });
    node.port.postMessage({type:'channel',value:$('channel-select').value});
    node.port.onmessage=(event)=>{
      const a=event.data;
      if(a?.type!=='audio'||state.mode!=='mic')return;
      if(a.selected!==state.selectedChannel||a.channels!==state.availableChannels){
        state.selectedChannel=a.selected;state.availableChannels=a.channels;
        $('input-extra').textContent=`STFT ${state.fft}/${state.hop} · CH ${a.selected}/${a.channels}`;
      }
      if(state.gapPending){state.droppedBlocks++;return;}
      if(state.inflight>=6){
        state.droppedBlocks++;state.gapPending=true;
        worker.postMessage({type:'gap',epoch:state.epoch});return;
      }
      state.inflight++;
      worker.postMessage({type:'audio',epoch:state.epoch,pcm:a.pcm},[a.pcm]);
    };
    state.stream=stream;state.context=ctx;state.source=source;state.node=node;
    state.fs=ctx.sampleRate;state.fft=Number($('fft-select').value);
    setMode('mic');configureWorker();
    source.connect(node);node.connect(ctx.destination); // zero-output worklet: no sound playback
    await earlyResume;await ctx.resume();
    if(ctx.state!=='running')throw Error('AudioContext did not start. Tap Start microphone again.');
    const track=stream.getAudioTracks()[0];
    track.addEventListener('ended',()=>{
      if(state.mode==='mic')void stopAll().then(()=>msg('Microphone disconnected. Tap Start to reconnect.','warning'));
    });
    const s=detectSettings(track);
    $('input-name').textContent=track.label||'Live microphone';
    $('input-meta').textContent=`${ctx.sampleRate} Hz · ${s.channelCount||'unknown'} reported channel(s) · output muted`;
    await refreshDevices();
    if(!$('message').classList.contains('warning'))msg('Live capture started. All analysis stays on your device.');
  }catch(e){
    if(stream)stream.getTracks().forEach(t=>t.stop());
    if(ctx){try{await ctx.close();}catch{}}
    state.stream=null;state.context=null;state.source=null;state.node=null;
    setMode('idle');
    let reason=String(e?.message||e);
    if(e?.name==='NotAllowedError')reason='Microphone permission denied. Enable it in Safari site settings and try again.';
    if(e?.name==='NotReadableError')reason='Microphone is unavailable or used exclusively by another app.';
    msg(`Microphone not started: ${reason}`,'error');
  }finally{state.working=false;buttons();}
}
function axisX(freq,px,min,max,log){
  return log?px*(Math.log(Math.max(freq,min)/min)/Math.log(max/min)):px*(freq-min)/(max-min);
}
function frequencyTicks(max,log){
  const all=[40,50,100,200,500,1000,2000,5000,10000,20000];
  return log?all.filter(f=>f<max):[0,.25,.5,.75,1].map(a=>Math.round(max*a));
}
function freqLabel(f){if(f===0)return '0';if(f>=1000)return `${+(f/1000).toFixed(1)}k`;return String(Math.round(f));}
function fitCanvas(canvas){
  const rect=canvas.getBoundingClientRect();
  const pixelRatio=Math.min(window.devicePixelRatio||1,1.6);
  const w=Math.max(240,Math.round(rect.width*pixelRatio));
  const h=Math.max(120,Math.round(rect.height*pixelRatio));
  if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;state.dirty=true;}
}
function drawAxes(ctx,w,h,plot,fmax,log,vertical=false){
  const font=Math.max(10,Math.round(w/110));
  ctx.font=`${font}px system-ui,sans-serif`;
  ctx.fillStyle='#8aa8bf';ctx.strokeStyle='rgba(90,126,158,.3)';ctx.lineWidth=1;
  if(vertical){
    for(const f of frequencyTicks(fmax,log).filter(f=>f>=40)){
      const t=log?Math.log(fmax/f)/Math.log(fmax/40):1-f/fmax;
      const y=plot.y+plot.h*t;
      if(y<plot.y||y>plot.y+plot.h)continue;
      ctx.beginPath();ctx.moveTo(plot.x,y);ctx.lineTo(plot.x+plot.w,y);ctx.stroke();
      ctx.fillText(freqLabel(f),2,y+font*.35);
    }
  }else{
    for(const f of frequencyTicks(fmax,log)){
      if(log&&f<40)continue;
      const x=plot.x+axisX(f,plot.w,log?40:0,fmax,log);
      if(x<plot.x||x>plot.x+plot.w)continue;
      ctx.beginPath();ctx.moveTo(x,plot.y);ctx.lineTo(x,plot.y+plot.h);ctx.stroke();
      ctx.textAlign='center';ctx.fillText(freqLabel(f),x,h-3);
    }
    ctx.textAlign='left';
  }
}
function drawSpectrum(){
  const c=canvasSpec,ctx=specCtx,w=c.width,h=c.height;
  ctx.fillStyle='#0b1423';ctx.fillRect(0,0,w,h);
  const dpr=w/Math.max(1,c.getBoundingClientRect().width),plot={x:41*dpr,y:9*dpr,w:w-51*dpr,h:h-32*dpr};
  if(plot.w<5||plot.h<5)return;
  const fmax=Math.min(20000,state.fs/2),log=state.axis==='log';
  ctx.strokeStyle='#22344a';ctx.strokeRect(plot.x,plot.y,plot.w,plot.h);
  drawAxes(ctx,w,h,plot,fmax,log);
  for(const db of [-20,-40,-60,-80,-100]){
    const y=plot.y+plot.h*(state.ceil-db)/(state.ceil-state.floor);
    if(y<plot.y||y>plot.y+plot.h)continue;
    ctx.strokeStyle='rgba(85,126,159,.3)';ctx.beginPath();ctx.moveTo(plot.x,y);ctx.lineTo(plot.x+plot.w,y);ctx.stroke();
    ctx.font=`${Math.max(9,10*dpr)}px system-ui`;ctx.fillStyle='#8fabbd';ctx.textAlign='left';ctx.fillText(String(db),3,y-2);
  }
  const data=state.latest;if(!data)return;
  const binHz=state.fs/state.fft;
  ctx.strokeStyle='#58dcd8';ctx.lineWidth=Math.max(1,1.25*dpr);ctx.beginPath();
  let started=false;
  for(let k=1;k<data.length;k++){
    const freq=k*binHz;if(freq>(fmax)||log&&freq<40)continue;
    const x=plot.x+axisX(freq,plot.w,log?40:0,fmax,log);
    const value=Math.max(state.floor,Math.min(state.ceil,data[k]));
    const y=plot.y+plot.h*(state.ceil-value)/(state.ceil-state.floor);
    if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);
  }
  ctx.stroke();
  if(state.metrics?.voiced){
    const f0=state.metrics.f0;
    for(let harm=1;harm<=8;harm++){
      const f=f0*harm;if(f>fmax)break;
      const x=plot.x+axisX(f,plot.w,log?40:0,fmax,log);
      if(x<plot.x||x>plot.x+plot.w)continue;
      ctx.strokeStyle=harm===1?'rgba(252,224,139,.9)':'rgba(252,224,139,.27)';
      ctx.setLineDash(harm===1?[4*dpr,3*dpr]:[2*dpr,6*dpr]);
      ctx.beginPath();ctx.moveTo(x,plot.y);ctx.lineTo(x,plot.y+plot.h);ctx.stroke();
    }
    ctx.setLineDash([]);
  }
}
function drawSpectrogram(){
  const c=canvasGram,ctx=gramCtx,w=c.width,h=c.height,dpr=w/Math.max(1,c.getBoundingClientRect().width);
  ctx.fillStyle='#0b1423';ctx.fillRect(0,0,w,h);
  const plot={x:41*dpr,y:8*dpr,w:Math.floor(w-49*dpr),h:Math.floor(h-30*dpr)};
  if(plot.w<2||plot.h<2)return;
  const maxF=Math.min(20000,state.fs/2),minF=state.axis==='log'?40:0,log=state.axis==='log';
  const pixels=ctx.createImageData(plot.w,plot.h);
  for(let p=0;p<pixels.data.length;p+=4){
    pixels.data[p]=8;pixels.data[p+1]=15;pixels.data[p+2]=26;pixels.data[p+3]=255;
  }
  const frameCount=state.history.length;
  if(frameCount){
    const rows=new Float64Array(plot.h);
    for(let y=0;y<plot.h;y++){
      const t=1-y/Math.max(1,plot.h-1);
      const f=log?minF*Math.pow(maxF/minF,t):maxF*t;
      rows[y]=Math.min(state.fft/2,Math.max(0,f/state.fs*state.fft));
    }
    const first=state.history[0],latest=state.history.at(-1),cutoff=latest.t-state.seconds;
    let j=0;
    for(let x=0;x<plot.w;x++){
      const time=cutoff+(x/(plot.w-1))*state.seconds;
      while(j+1<frameCount&&state.history[j+1].t<=time)j++;
      if(time<first.t || time-state.history[j].t>2.2*state.hop/state.fs)continue;
      const db=state.history[j].db;
      for(let y=0;y<plot.h;y++){
        const bin=rows[y],k=Math.floor(bin),fraction=bin-k;
        const val=db[k]*(1-fraction)+db[Math.min(k+1,db.length-1)]*fraction;
        const idx=Math.max(0,Math.min(255,Math.round(255*(val-state.floor)/(state.ceil-state.floor))));
        const dest=(y*plot.w+x)*4,src=idx*4;
        pixels.data[dest]=PALETTE[src];pixels.data[dest+1]=PALETTE[src+1];
        pixels.data[dest+2]=PALETTE[src+2];pixels.data[dest+3]=255;
      }
    }
  }
  ctx.putImageData(pixels,Math.round(plot.x),Math.round(plot.y));
  ctx.strokeStyle='#36465b';ctx.strokeRect(plot.x,plot.y,plot.w,plot.h);
  drawAxes(ctx,w,h,plot,maxF,log,true);
  ctx.font=`${Math.max(9,10*dpr)}px system-ui`;ctx.textAlign='center';ctx.fillStyle='#8fa9c0';
  for(const sec of [state.seconds,Math.round(state.seconds*.75),Math.round(state.seconds*.5),Math.round(state.seconds*.25),0]){
    const x=plot.x+plot.w*(1-sec/state.seconds);
    ctx.strokeStyle='rgba(100,140,169,.16)';ctx.beginPath();ctx.moveTo(x,plot.y);ctx.lineTo(x,plot.y+plot.h);ctx.stroke();
    ctx.fillText(sec===0?'now':`−${sec}s`,x,h-5);
  }
  ctx.textAlign='left';
}
let trackedDraws=0,lastFpsMark=performance.now();
function render(now){
  requestAnimationFrame(render);
  if(!state.dirty || now-state.lastDrawAt<33)return;
  fitCanvas(canvasSpec);fitCanvas(canvasGram);
  const start=performance.now();
  drawSpectrum();drawSpectrogram();
  const elapsed=performance.now()-start;
  state.lastDrawMs=elapsed;state.dirty=false;state.lastDrawAt=now;
  trackedDraws++;
  if(now-lastFpsMark>1000){
    const fps=trackedDraws*1000/(now-lastFpsMark);
    $('analyzer-fps').textContent=`DISPLAY ${fps.toFixed(0)} FPS · DRAW ${elapsed.toFixed(1)} ms`;
    trackedDraws=0;lastFpsMark=now;
  }
}
function resetSettings(){
  state.fft=Number($('fft-select').value);
  state.axis=$('axis-select').value;state.seconds=Number($('history-select').value);
  state.floor=Number($('floor-select').value);
  $('scale-min').textContent=String(state.floor);
  $('scale-max').textContent=String(state.ceil);
  $('gram-meta').textContent=`${state.axis.toUpperCase()} FREQUENCY · ${state.seconds} s`;
  state.dirty=true;
}
$('start-btn').addEventListener('click',()=>void startMic());
$('demo-btn').addEventListener('click',()=>void startDemo());
$('stop-btn').addEventListener('click',()=>void stopAll());
$('clear-btn').addEventListener('click',()=>{cleanMeasurements();worker.postMessage({type:'clear'});msg('History cleared.');});
$('axis-select').addEventListener('change',resetSettings);
$('history-select').addEventListener('change',resetSettings);
$('floor-select').addEventListener('change',resetSettings);
$('fft-select').addEventListener('change',()=>{
  resetSettings();if(state.mode!=='idle')configureWorker();
  msg('STFT window updated; analysis history was reset.');
});
$('channel-select').addEventListener('change',()=>{
  state.node?.port.postMessage({type:'channel',value:$('channel-select').value});
  msg(`Requested ${$('channel-select').selectedOptions[0].text}.`);
});
$('device-select').addEventListener('change',()=>{if(state.mode==='mic')msg('Press Stop, then Start microphone to select the new input.','warning');});
$('snapshot-btn').addEventListener('click',()=>{
  if(!canvasGram.width)return;
  canvasGram.toBlob(blob=>{
    if(!blob){msg('Unable to save PNG from canvas.','warning');return;}
    const url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download=`spectra-${new Date().toISOString().replaceAll(':','-')}.png`;
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);
  },'image/png');
});
window.addEventListener('resize',()=>state.dirty=true);
new ResizeObserver(()=>state.dirty=true).observe(canvasGram);
new ResizeObserver(()=>state.dirty=true).observe(canvasSpec);
document.addEventListener('visibilitychange',()=>{
  if(document.hidden&&state.mode!=='idle')void stopAll().then(()=>msg('Audio stopped when app left foreground. Tap Start to resume.','warning'));
});
window.addEventListener('pagehide',()=>{
  if(state.stream)state.stream.getTracks().forEach(t=>t.stop());
  worker.postMessage({type:'stop-demo'});
});
if('serviceWorker' in navigator && isSecureContext){
  window.addEventListener('load',()=>navigator.serviceWorker.register('./sw.js').catch(()=>{}));
}
resetSettings();setMode('idle');buttons();refreshDevices();requestAnimationFrame(render);
