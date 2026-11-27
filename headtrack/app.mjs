const el = (id) => document.getElementById(id);
const state = {sensor:false, heading:null, zero:null, yaw:0, websocket:null, authenticated:false, lastSent:0, lastEvent:0, eventCount:0};
const wrap = (angle) => ((angle + 180) % 360 + 360) % 360 - 180;
const bounded = (x, low, high) => Math.max(low, Math.min(high, x));

function headingFromOrientation(event) {
  if (![event.alpha, event.beta, event.gamma].every(Number.isFinite)) return null;
  // W3C DeviceOrientation: R = R_z(alpha) R_x(beta) R_y(gamma).
  // Screen faces the wearer, so the device's -Z points forwards.
  const [a,b,g] = [event.alpha,event.beta,event.gamma].map(v => v*Math.PI/180);
  const x = -Math.sin(g)*Math.cos(a)-Math.sin(b)*Math.cos(g)*Math.sin(a);
  const y = -Math.sin(g)*Math.sin(a)+Math.sin(b)*Math.cos(g)*Math.cos(a);
  if (Math.hypot(x,y) < 0.35) {
    el('sensor-help').textContent = 'Erga o celular: a tela deve ficar vertical e voltada para você. A direção para frente não está definida quando a câmera traseira aponta para cima ou para baixo.';
    return null;
  }
  return Math.atan2(y,x)*180/Math.PI;
}
function updateYaw(value) {
  state.yaw = wrap(value);
  el('yaw-value').textContent = state.yaw.toFixed(1) + '°';
  el('needle').style.transform = 'rotate(' + state.yaw + 'deg)';
  const a = wrap(30-state.yaw), b = wrap(-30-state.yaw);
  const out = Math.abs(wrap(a-30)) > 15 || Math.abs(wrap(b+30)) > 15;
  el('geometry-warning').classList.toggle('out', out);
  el('geometry-warning').textContent = out
    ? 'Geometria fora do treino (+30° / −30°); a identidade das saídas pode falhar.'
    : 'Geometria próxima da condição de treino.';
  sendControls();
}
function orientation(event) {
  const h = headingFromOrientation(event);
  if (h === null) return;
  state.heading = h;
  const now = performance.now();
  state.eventCount += 1;
  if (now - state.lastEvent > 1000) {
    el('sensor-rate').textContent = String(Math.round(1000*state.eventCount/Math.max(1, now-state.lastEvent))) + ' Hz';
    state.lastEvent = now; state.eventCount = 0;
  }
  if (state.zero === null) state.zero = h;
  if (el('manual-mode').checked) return;
  let yaw = wrap(h-state.zero);
  if (el('invert').checked) yaw = -yaw;
  updateYaw(yaw);
}
async function activate() {
  if (typeof DeviceOrientationEvent === 'undefined') {
    el('sensor-help').textContent = 'Este navegador não disponibiliza DeviceOrientationEvent. Use o controle manual.';
    return;
  }
  try {
    if (typeof DeviceOrientationEvent.requestPermission === 'function') {
      const permission = await DeviceOrientationEvent.requestPermission();
      if (permission !== 'granted') throw new Error('Acesso negado aos sensores');
    }
    window.addEventListener('deviceorientation', orientation, {passive:true});
    state.sensor = true;
    el('sensor-state').textContent = 'SENSOR ATIVO';
    el('sensor-button').disabled = true;
    el('sensor-help').textContent = 'Olhe para frente, mantenha a tela voltada para você e toque em Calibrar frente. Gire a cabeça e confira se o sinal do yaw corresponde ao movimento. O telefone precisa acompanhar a cabeça fisicamente.';
  } catch (error) {
    el('sensor-state').textContent = 'SENSOR INDISPONÍVEL';
    el('sensor-help').textContent = String(error) + '. Use o controle manual ou verifique HTTPS e permissões.';
  }
}
function sendControls(force=false) {
  const now=performance.now();
  if (!force && now-state.lastSent < 50) return;
  const ws=state.websocket;
  if (!state.authenticated || !ws || ws.readyState!==WebSocket.OPEN) return;
  state.lastSent=now;
  ws.send(JSON.stringify({
    type:'control', yaw:bounded(state.yaw,-180,180),
    raw_on:el('raw-on').checked,
    target:Number(el('target').value),
    raw_gain:Number(el('dry-gain').value),
    enh_gain:Number(el('wet-gain').value)
  }));
}
function connect() {
  let endpoint;
  try {
    endpoint = new URL(el('endpoint').value.trim());
    if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw Error('Use apenas uma URL https:// do túnel');
  } catch (error) {
    el('telemetry').textContent='Endereço inválido. Cole o https://…trycloudflare.com mostrado pelo túnel.';
    return;
  }
  const token=el('session-token').value.trim();
  if (token.length < 20) { el('telemetry').textContent='Digite o código completo exibido no PC.'; return; }
  if (state.websocket) state.websocket.close();
  state.authenticated=false;
  el('bridge-state').textContent='CONECTANDO';
  const path=endpoint.pathname.replace(/\/+$/,'')+'/ws';
  const ws=new WebSocket('wss://'+endpoint.host+path);
  state.websocket=ws;
  ws.onopen=()=>ws.send(JSON.stringify({type:'auth',token}));
  ws.onmessage=(event)=>{
    let message;
    try{message=JSON.parse(event.data);}catch{return;}
    if(message.type==='ready'){
      state.authenticated=true;
      el('bridge-state').textContent='CONECTADO';
      el('disconnect').disabled=false;
      el('connect').disabled=true;
      el('telemetry').textContent='Áudio ativo no computador. Calibre a frente e use fones.';
      sendControls(true);
    }
    if(message.type==='status'){
      el('telemetry').textContent=
        'DSP: '+Number(message.compute_ms).toFixed(2)+' ms / bloco (prazo '+Number(message.deadline_ms).toFixed(2)+' ms)\n'+
        'Máximo: '+Number(message.max_ms).toFixed(2)+' ms · prazos perdidos: '+Number(message.deadline_misses)+' · underflows: '+Number(message.underflows)+'\n'+
        'Orientação recebida há '+Number(message.sensor_age_ms).toFixed(0)+' ms. Áudio nos fones do PC.';
    }
    if(message.type==='error') el('telemetry').textContent=String(message.message);
  };
  ws.onerror=()=>{el('telemetry').textContent='Falha WSS. Verifique o endereço, o túnel e se o programa está ativo.';};
  ws.onclose=()=>{
    if(state.websocket!==ws)return;
    state.authenticated=false;
    el('bridge-state').textContent='DESCONECTADO';
    el('connect').disabled=false;el('disconnect').disabled=true;
  };
}
el('sensor-button').addEventListener('click',activate);
el('zero-button').addEventListener('click',()=>{
  if(state.heading!==null)state.zero=state.heading;
  updateYaw(el('manual-mode').checked?Number(el('manual-yaw').value):0);
});
el('manual-mode').addEventListener('change',()=>{
  el('manual-yaw').disabled=!el('manual-mode').checked;
  if(el('manual-mode').checked)updateYaw(Number(el('manual-yaw').value));
  else if(state.heading!==null)updateYaw((el('invert').checked?-1:1)*wrap(state.heading-state.zero));
});
el('manual-yaw').addEventListener('input',()=>{el('manual-value').textContent=el('manual-yaw').value+'°';updateYaw(Number(el('manual-yaw').value));});
el('invert').addEventListener('change',()=>{if(state.heading!==null&&!el('manual-mode').checked)updateYaw((el('invert').checked?-1:1)*wrap(state.heading-state.zero));});
for(const id of ['raw-on','target','dry-gain','wet-gain']){
  el(id).addEventListener(id.includes('gain')?'input':'change',()=>{
    el('dry-value').textContent=Number(el('dry-gain').value).toFixed(2);
    el('wet-value').textContent=Number(el('wet-gain').value).toFixed(2);
    sendControls(true);
  });
}
el('connect').addEventListener('click',connect);
el('disconnect').addEventListener('click',()=>{state.websocket?.close();});
window.addEventListener('pagehide',()=>state.websocket?.close());